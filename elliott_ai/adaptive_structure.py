"""Deterministic structural contracts for adaptive Elliott recounts.

The objects in this module deliberately separate an analysis window from an
Elliott wave.  They are pure, immutable, content-hashed contracts and perform
no provider calls, market-data retrieval, persistence, forecasting, or status
promotion outside the shadow recount.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .forecast_records import canonical_sha256
from .schema import STANDARD_DEGREES


ADAPTIVE_ANALYSIS_SCOPE_SCHEMA_VERSION = "adaptive-analysis-scope-1.0.0"
ADAPTIVE_PRICE_SEGMENTATION_LEGACY_SCHEMA_VERSION = (
    "adaptive-price-segmentation-2.0.0"
)
ADAPTIVE_PRICE_SEGMENTATION_SCHEMA_VERSION = "adaptive-price-segmentation-3.0.0"
ADAPTIVE_SEGMENT_CLASSIFICATION_SCHEMA_VERSION = (
    "adaptive-segment-classification-1.0.0"
)
ADAPTIVE_ROOT_STRUCTURAL_GROUPING_SCHEMA_VERSION = (
    "adaptive-root-structural-grouping-1.0.0"
)
ADAPTIVE_GROUP_CLASSIFICATION_SCHEMA_VERSION = (
    "adaptive-group-classification-1.0.0"
)
ADAPTIVE_BOUNDARY_VALIDITY_SCHEMA_VERSION = "adaptive-boundary-validity-1.0.0"
ADAPTIVE_HARD_RULE_CALCULATION_SCHEMA_VERSION = (
    "adaptive-hard-rule-calculation-1.0.0"
)
ADAPTIVE_DIAGONAL_RULE_POLICY_VERSION = (
    "adaptive-diagonal-rules-leading-53535-ending-zigzag-five-2.0.0"
)
ADAPTIVE_DIAGONAL_INTERNAL_FAMILY_POLICY_VERSION = (
    "adaptive-diagonal-internal-families-leading-53535-ending-zigzag-five-1.0.0"
)
ADAPTIVE_DIAGONAL_POLICY_SOURCE = "repository-approved-active-standard-doctrine"
ADAPTIVE_CHILD_RECONSTRUCTION_POLICY_VERSION = (
    "adaptive-child-reconstruction-policy-family-locked-1.2.0"
)
ADAPTIVE_PARENT_STRUCTURAL_ASSESSMENT_SCHEMA_VERSION = (
    "adaptive-parent-structural-assessment-1.1.0"
)
ADAPTIVE_PARENT_IMPOSSIBILITY_POLICY_VERSION = (
    "adaptive-parent-impossibility-direct-hard-rule-only-1.0.0"
)
FAMILY_EXHAUSTIVE_IMPOSSIBILITY_SUPPORTED = False

MAX_CHILD_CANDIDATES_PER_PARENT = 2
# One initial graph plus at most two materially distinct reconstruction graphs.
MAX_CHILD_RECOUNTS_PER_PARENT = 1 + MAX_CHILD_CANDIDATES_PER_PARENT

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_EPSILON = 1e-9


class AnalysisBoundaryKind(StrEnum):
    WINDOW_START = "window_start"
    CATALOG_PIVOT = "catalog_pivot"
    AS_OF_OBSERVATION = "as_of_observation"


class SegmentCompletionState(StrEnum):
    CLOSED = "closed"
    ACTIVE = "active"
    UNRESOLVED = "unresolved"


class PriceExtremumType(StrEnum):
    HIGH = "high"
    LOW = "low"


class PriceSegmentKind(StrEnum):
    ATOMIC_SWING = "atomic_swing"
    CONTINUATION_INTERVAL = "continuation_interval"
    BOUNDARY_CONTEXT = "boundary_context"
    UNRESOLVED_INTERVAL = "unresolved_interval"


class SegmentClassificationKind(StrEnum):
    IMPULSE = "impulse"
    LEADING_DIAGONAL = "leading_diagonal"
    ENDING_DIAGONAL = "ending_diagonal"
    ABC = "abc"
    ZIGZAG = "zigzag"
    WXY = "wxy"
    WXYXZ = "wxyxz"
    FLAT = "flat"
    TRIANGLE = "triangle"
    COMBINATION = "combination"
    PREVIOUS_PARENT_WAVE_TAIL = "previous_parent_wave_tail"
    UNFINISHED_STRUCTURE = "unfinished_structure"
    DEGREE_TRANSITION = "degree_transition"
    UNRESOLVED = "unresolved"


class BoundaryValidityStatus(StrEnum):
    VALID = "valid"
    BOUNDARY_REQUIRES_RESELECTION = "boundary_requires_reselection"
    PIVOT_NOT_TRUE_EXTREME = "pivot_not_true_extreme"
    CANDIDATE_SEGMENTATION_FAILED = "candidate_segmentation_failed"


class StructuralInvalidationRule(StrEnum):
    WAVE_2_BEYOND_WAVE_1_ORIGIN = "wave_2_beyond_wave_1_origin"
    WAVE_3_FAILED_TO_EXCEED_WAVE_1_EXTREME = (
        "wave_3_failed_to_exceed_wave_1_extreme"
    )
    WAVE_3_IS_SHORTEST = "wave_3_is_shortest"
    STANDARD_IMPULSE_WAVE_4_OVERLAPS_WAVE_1 = (
        "standard_impulse_wave_4_overlaps_wave_1"
    )
    DIAGONAL_WAVE_4_OVERLAP_MISSING = "diagonal_wave_4_overlap_missing"
    DIAGONAL_GEOMETRY_MISMATCH = "diagonal_geometry_mismatch"
    DIAGONAL_CHILD_FAMILY_MISMATCH = "diagonal_child_family_mismatch"
    DIAGONAL_WAVE_5_TERMINATION_MISMATCH = (
        "diagonal_wave_5_termination_mismatch"
    )


class DiagonalType(StrEnum):
    LEADING = "leading"
    ENDING = "ending"


class DiagonalGeometry(StrEnum):
    CONTRACTING = "contracting"
    EXPANDING = "expanding"


class DiagonalWave5Termination(StrEnum):
    NORMAL = "normal"
    TRUNCATED = "truncated"
    THROW_OVER = "throw_over"


class AdaptiveChildAttemptDisposition(StrEnum):
    VERIFIED = "verified"
    UNPROVEN = "unproven"
    NOT_COVERED = "not_covered"
    CHILD_CANDIDATE_REJECTED = "child_candidate_rejected"
    BOUNDARY_REQUIRES_RESELECTION = "boundary_requires_reselection"
    PIVOT_NOT_TRUE_EXTREME = "pivot_not_true_extreme"
    CANDIDATE_SEGMENTATION_FAILED = "candidate_segmentation_failed"
    DUPLICATE_RECONSTRUCTION_CANDIDATE = "duplicate_reconstruction_candidate"
    PARENT_FAMILY_RECLASSIFICATION_REQUIRED = (
        "parent_family_reclassification_required"
    )


class AdaptiveParentProofDisposition(StrEnum):
    VERIFIED = "verified"
    UNPROVEN = "unproven"
    NOT_COVERED = "not_covered"
    INCONSISTENT = "inconsistent"


class AdaptiveSearchOutcome(StrEnum):
    VERIFIED = "verified"
    UNPROVEN = "unproven"
    NOT_COVERED = "not_covered"
    RECOUNT_REQUIRED = "recount_required"
    CANDIDATE_SEARCH_EXHAUSTED = "candidate_search_exhausted"
    PARENT_STRUCTURALLY_INCONSISTENT = "parent_structurally_inconsistent"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    method = getattr(value, "to_dict", None)
    if callable(method):
        return _json_value(method())
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Canonical JSON cannot contain non-finite values.")
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
            raise ValueError("Immutable JSON cannot contain non-finite values.")
        return value
    method = getattr(value, "to_dict", None)
    if callable(method):
        return _freeze_json(method())
    raise TypeError(f"Unsupported immutable value: {type(value).__name__}.")


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _hash(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name).lower()
    if _HASH_PATTERN.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return text


def _utc_text(value: Any, *, field_name: str) -> str:
    raw = _text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _finite(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite and numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite and numeric.")
    return result


class _Contract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class AnalysisScopeBoundary(_Contract):
    boundary_id: str
    kind: AnalysisBoundaryKind
    timestamp_utc: str
    price: float
    source_pivot_id: str | None
    source_bar_hash: str
    is_elliott_pivot: bool
    extremum_type: PriceExtremumType | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "boundary_id", _text(self.boundary_id, field_name="boundary_id"))
        object.__setattr__(self, "kind", AnalysisBoundaryKind(self.kind))
        object.__setattr__(self, "timestamp_utc", _utc_text(self.timestamp_utc, field_name="timestamp_utc"))
        object.__setattr__(self, "price", _finite(self.price, field_name="price"))
        object.__setattr__(self, "source_bar_hash", _hash(self.source_bar_hash, field_name="source_bar_hash"))
        if self.source_pivot_id is not None:
            object.__setattr__(self, "source_pivot_id", _text(self.source_pivot_id, field_name="source_pivot_id"))
        if not isinstance(self.is_elliott_pivot, bool):
            raise TypeError("is_elliott_pivot must be boolean.")
        if self.extremum_type is not None:
            object.__setattr__(
                self,
                "extremum_type",
                PriceExtremumType(self.extremum_type),
            )
        if self.kind is AnalysisBoundaryKind.CATALOG_PIVOT:
            if self.source_pivot_id is None or not self.is_elliott_pivot:
                raise ValueError("Catalog-pivot boundaries require explicit pivot lineage.")
        elif (
            self.source_pivot_id is not None
            or self.is_elliott_pivot
            or self.extremum_type is not None
        ):
            raise ValueError("Window and as-of boundaries are observations, not Elliott pivots.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnalysisScopeBoundary":
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.extremum_type is None:
            payload.pop("extremum_type", None)
        return payload


@dataclass(frozen=True, slots=True)
class AdaptiveAnalysisScope(_Contract):
    scope_id: str
    symbol: str
    timeframe: str
    window_start: AnalysisScopeBoundary
    as_of_observation: AnalysisScopeBoundary
    analysis_cutoff_utc: str
    source_bundle_hashes: tuple[str, ...]
    schema_version: str = ADAPTIVE_ANALYSIS_SCOPE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _text(self.scope_id, field_name="scope_id"))
        object.__setattr__(self, "symbol", _text(self.symbol, field_name="symbol"))
        object.__setattr__(self, "timeframe", _text(self.timeframe, field_name="timeframe"))
        if not isinstance(self.window_start, AnalysisScopeBoundary) or not isinstance(
            self.as_of_observation, AnalysisScopeBoundary
        ):
            raise TypeError("Scope boundaries must be AnalysisScopeBoundary values.")
        if self.window_start.kind is not AnalysisBoundaryKind.WINDOW_START:
            raise ValueError("window_start must use window_start boundary kind.")
        if self.as_of_observation.kind is not AnalysisBoundaryKind.AS_OF_OBSERVATION:
            raise ValueError("as_of_observation must use as_of_observation boundary kind.")
        if _utc(self.as_of_observation.timestamp_utc) <= _utc(self.window_start.timestamp_utc):
            raise ValueError("Analysis scope must have positive duration.")
        object.__setattr__(self, "analysis_cutoff_utc", _utc_text(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        if _utc(self.as_of_observation.timestamp_utc) > _utc(self.analysis_cutoff_utc):
            raise ValueError("Scope as-of observation cannot exceed its cutoff.")
        hashes = tuple(_hash(item, field_name="source_bundle_hash") for item in self.source_bundle_hashes)
        if not hashes or len(hashes) != len(set(hashes)):
            raise ValueError("source_bundle_hashes must be non-empty and unique.")
        object.__setattr__(self, "source_bundle_hashes", hashes)
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and self.content_hash != adaptive_analysis_scope_content_hash(self):
            raise ValueError("AdaptiveAnalysisScope content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveAnalysisScope":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("scope_id"):
            values["scope_id"] = "adaptive_scope_" + canonical_sha256(
                {
                    "symbol": values.get("symbol"),
                    "start": _json_value(values.get("window_start")),
                    "as_of": _json_value(values.get("as_of_observation")),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=adaptive_analysis_scope_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "AdaptiveAnalysisScope":
        raw = dict(value)
        raw["window_start"] = AnalysisScopeBoundary.from_dict(raw["window_start"])
        raw["as_of_observation"] = AnalysisScopeBoundary.from_dict(raw["as_of_observation"])
        raw["source_bundle_hashes"] = tuple(raw.get("source_bundle_hashes", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_analysis_scope_content_hash(result):
            raise ValueError("AdaptiveAnalysisScope content_hash does not match its payload.")
        return result


def adaptive_analysis_scope_content_hash(value: AdaptiveAnalysisScope | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveAnalysisScope) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class PriceSegmentCandidate(_Contract):
    segment_id: str
    start_boundary: AnalysisScopeBoundary
    end_boundary: AnalysisScopeBoundary
    completion_state: SegmentCompletionState
    segment_kind: PriceSegmentKind | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "segment_id", _text(self.segment_id, field_name="segment_id"))
        if not isinstance(self.start_boundary, AnalysisScopeBoundary) or not isinstance(
            self.end_boundary, AnalysisScopeBoundary
        ):
            raise TypeError("Segment boundaries must be AnalysisScopeBoundary values.")
        if _utc(self.end_boundary.timestamp_utc) <= _utc(self.start_boundary.timestamp_utc):
            raise ValueError("Price segment end must follow its start.")
        object.__setattr__(self, "completion_state", SegmentCompletionState(self.completion_state))
        if self.segment_kind is not None:
            object.__setattr__(self, "segment_kind", PriceSegmentKind(self.segment_kind))
        if (
            self.segment_kind is PriceSegmentKind.ATOMIC_SWING
            and self.completion_state is SegmentCompletionState.CLOSED
        ):
            if (
                self.start_boundary.kind is not AnalysisBoundaryKind.CATALOG_PIVOT
                or self.end_boundary.kind is not AnalysisBoundaryKind.CATALOG_PIVOT
                or self.start_boundary.extremum_type is None
                or self.end_boundary.extremum_type is None
            ):
                raise ValueError(
                    "A closed atomic_swing requires two typed catalog-pivot extrema."
                )
            if self.start_boundary.extremum_type is self.end_boundary.extremum_type:
                raise ValueError(
                    "Closed atomic_swing endpoints must alternate high and low extrema."
                )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PriceSegmentCandidate":
        raw = dict(value)
        raw["start_boundary"] = AnalysisScopeBoundary.from_dict(raw["start_boundary"])
        raw["end_boundary"] = AnalysisScopeBoundary.from_dict(raw["end_boundary"])
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.segment_kind is None:
            payload.pop("segment_kind", None)
        return payload


@dataclass(frozen=True, slots=True)
class PriceSegmentationHypothesis(_Contract):
    segmentation_id: str
    role: str
    analysis_scope_hash: str
    segments: tuple[PriceSegmentCandidate, ...]
    provider: str
    model: str | None
    generated_at_utc: str
    candidate_state: str = "candidate"
    schema_version: str = ADAPTIVE_PRICE_SEGMENTATION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "segmentation_id", _text(self.segmentation_id, field_name="segmentation_id"))
        role = _text(self.role, field_name="role").lower()
        if role not in {"primary", "alternative"}:
            raise ValueError("role must be primary or alternative.")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "analysis_scope_hash", _hash(self.analysis_scope_hash, field_name="analysis_scope_hash"))
        segments = tuple(self.segments)
        if not segments or any(not isinstance(item, PriceSegmentCandidate) for item in segments):
            raise ValueError("A segmentation requires typed price segments.")
        if len({item.segment_id for item in segments}) != len(segments):
            raise ValueError("Price segment IDs must be unique.")
        for left, right in zip(segments, segments[1:]):
            if left.end_boundary.boundary_id != right.start_boundary.boundary_id:
                raise ValueError("Price segmentation cannot skip or disconnect an interval.")
            if left.completion_state is not SegmentCompletionState.CLOSED:
                raise ValueError("Only the final price segment may be active or unresolved.")
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        object.__setattr__(self, "generated_at_utc", _utc_text(self.generated_at_utc, field_name="generated_at_utc"))
        if self.candidate_state != "candidate":
            raise ValueError("Price segmentation remains candidate-only.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and self.content_hash != price_segmentation_content_hash(self):
            raise ValueError("PriceSegmentationHypothesis content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "PriceSegmentationHypothesis":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=price_segmentation_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "PriceSegmentationHypothesis":
        raw = dict(value)
        raw["segments"] = tuple(PriceSegmentCandidate.from_dict(item) for item in raw["segments"])
        result = cls(**raw)
        if verify_hash and result.content_hash != price_segmentation_content_hash(result):
            raise ValueError("PriceSegmentationHypothesis content_hash does not match its payload.")
        return result


def price_segmentation_content_hash(value: PriceSegmentationHypothesis | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, PriceSegmentationHypothesis) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class StructuralSegmentGroup(_Contract):
    group_id: str
    source_segment_ids: tuple[str, ...]
    start_boundary: AnalysisScopeBoundary
    end_boundary: AnalysisScopeBoundary
    completion_state: SegmentCompletionState

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _text(self.group_id, field_name="group_id"))
        segment_ids = tuple(
            _text(item, field_name="source_segment_id")
            for item in self.source_segment_ids
        )
        if not segment_ids or len(segment_ids) != len(set(segment_ids)):
            raise ValueError("A structural group requires unique source segment IDs.")
        object.__setattr__(self, "source_segment_ids", segment_ids)
        if not isinstance(self.start_boundary, AnalysisScopeBoundary) or not isinstance(
            self.end_boundary, AnalysisScopeBoundary
        ):
            raise TypeError("Structural group boundaries must be typed scope boundaries.")
        if _utc(self.end_boundary.timestamp_utc) <= _utc(self.start_boundary.timestamp_utc):
            raise ValueError("Structural group end must follow its start.")
        object.__setattr__(
            self,
            "completion_state",
            SegmentCompletionState(self.completion_state),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StructuralSegmentGroup":
        raw = dict(value)
        raw["source_segment_ids"] = tuple(raw.get("source_segment_ids", ()))
        raw["start_boundary"] = AnalysisScopeBoundary.from_dict(raw["start_boundary"])
        raw["end_boundary"] = AnalysisScopeBoundary.from_dict(raw["end_boundary"])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class RootStructuralGroupingHypothesis(_Contract):
    grouping_id: str
    role: str
    segmentation_content_hash: str
    groups: tuple[StructuralSegmentGroup, ...]
    unresolved_segment_ids: tuple[str, ...]
    provider: str
    model: str | None
    generated_at_utc: str
    candidate_state: str = "candidate"
    schema_version: str = ADAPTIVE_ROOT_STRUCTURAL_GROUPING_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "grouping_id", _text(self.grouping_id, field_name="grouping_id"))
        role = _text(self.role, field_name="role").lower()
        if role not in {"primary", "alternative"}:
            raise ValueError("role must be primary or alternative.")
        object.__setattr__(self, "role", role)
        object.__setattr__(
            self,
            "segmentation_content_hash",
            _hash(self.segmentation_content_hash, field_name="segmentation_content_hash"),
        )
        groups = tuple(self.groups)
        if any(not isinstance(item, StructuralSegmentGroup) for item in groups):
            raise TypeError("groups must contain StructuralSegmentGroup values.")
        if len({item.group_id for item in groups}) != len(groups):
            raise ValueError("Structural group IDs must be unique.")
        object.__setattr__(self, "groups", groups)
        unresolved = tuple(
            _text(item, field_name="unresolved_segment_id")
            for item in self.unresolved_segment_ids
        )
        if len(unresolved) != len(set(unresolved)):
            raise ValueError("unresolved_segment_ids cannot repeat.")
        object.__setattr__(self, "unresolved_segment_ids", unresolved)
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        object.__setattr__(
            self,
            "generated_at_utc",
            _utc_text(self.generated_at_utc, field_name="generated_at_utc"),
        )
        if self.candidate_state != "candidate":
            raise ValueError("Root structural groupings remain candidate-only.")
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and self.content_hash != root_structural_grouping_content_hash(self):
            raise ValueError("RootStructuralGroupingHypothesis content_hash does not match its payload.")

    def validate_against(self, segmentation: PriceSegmentationHypothesis) -> None:
        if not isinstance(segmentation, PriceSegmentationHypothesis):
            raise TypeError("segmentation must be a PriceSegmentationHypothesis.")
        if self.segmentation_content_hash != segmentation.content_hash:
            raise ValueError("Structural grouping belongs to another segmentation.")
        if self.role != segmentation.role:
            raise ValueError("Structural grouping role does not match its segmentation.")
        source = tuple(segmentation.segments)
        index_by_id = {item.segment_id: index for index, item in enumerate(source)}
        grouped_ids: list[str] = []
        group_ranges: list[tuple[int, int]] = []
        group_spans: set[tuple[str, ...]] = set()
        for group in self.groups:
            if any(item not in index_by_id for item in group.source_segment_ids):
                raise ValueError("Structural group references an unavailable source segment.")
            indexes = tuple(index_by_id[item] for item in group.source_segment_ids)
            if indexes != tuple(range(indexes[0], indexes[-1] + 1)):
                raise ValueError("Structural group source segments must be ordered and contiguous.")
            if group.source_segment_ids in group_spans:
                raise ValueError("Structural grouping cannot repeat an identical segment span.")
            group_spans.add(group.source_segment_ids)
            group_ranges.append((indexes[0], indexes[-1]))
            first = source[indexes[0]]
            last = source[indexes[-1]]
            if group.start_boundary.to_dict() != first.start_boundary.to_dict():
                raise ValueError("Structural group start does not match its first source segment.")
            if group.end_boundary.to_dict() != last.end_boundary.to_dict():
                raise ValueError("Structural group end does not match its final source segment.")
            if group.completion_state is SegmentCompletionState.CLOSED:
                if any(
                    source[index].completion_state is not SegmentCompletionState.CLOSED
                    for index in indexes
                ):
                    raise ValueError("A closed structural group cannot contain an open source segment.")
            elif indexes[-1] != len(source) - 1:
                raise ValueError("Only a structural group ending at the final segment may remain open.")
            grouped_ids.extend(group.source_segment_ids)
        if group_ranges != sorted(group_ranges):
            raise ValueError("Structural group proposals must preserve deterministic source order.")
        unresolved = self.unresolved_segment_ids
        if any(item not in index_by_id for item in unresolved):
            raise ValueError("unresolved_segment_ids reference unavailable source segments.")
        if tuple(sorted(unresolved, key=index_by_id.__getitem__)) != unresolved:
            raise ValueError("unresolved_segment_ids must preserve source order.")
        if set(grouped_ids) & set(unresolved):
            raise ValueError("A source segment cannot be grouped and unresolved simultaneously.")
        if set((*grouped_ids, *unresolved)) != set(index_by_id):
            raise ValueError("Every Stage-A segment must be grouped or explicitly unresolved.")

    @classmethod
    def create(cls, **values: Any) -> "RootStructuralGroupingHypothesis":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=root_structural_grouping_content_hash(result))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        verify_hash: bool = True,
    ) -> "RootStructuralGroupingHypothesis":
        raw = dict(value)
        raw["groups"] = tuple(StructuralSegmentGroup.from_dict(item) for item in raw.get("groups", ()))
        raw["unresolved_segment_ids"] = tuple(raw.get("unresolved_segment_ids", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != root_structural_grouping_content_hash(result):
            raise ValueError("RootStructuralGroupingHypothesis content_hash does not match its payload.")
        return result


def root_structural_grouping_content_hash(
    value: RootStructuralGroupingHypothesis | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, RootStructuralGroupingHypothesis) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class GroupClassification(_Contract):
    group_id: str
    classification: SegmentClassificationKind
    wave_id: str | None
    evidence_ids: tuple[str, ...] = ()
    schema_version: str = ADAPTIVE_GROUP_CLASSIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _text(self.group_id, field_name="group_id"))
        object.__setattr__(
            self,
            "classification",
            SegmentClassificationKind(self.classification),
        )
        if self.wave_id is not None:
            object.__setattr__(self, "wave_id", _text(self.wave_id, field_name="wave_id"))
        evidence_ids = tuple(
            _text(item, field_name="evidence_id") for item in self.evidence_ids
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_ids cannot repeat.")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GroupClassification":
        raw = dict(value)
        raw["evidence_ids"] = tuple(raw.get("evidence_ids", ()))
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class SegmentClassification(_Contract):
    segment_id: str
    classification: SegmentClassificationKind
    wave_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "segment_id", _text(self.segment_id, field_name="segment_id"))
        object.__setattr__(self, "classification", SegmentClassificationKind(self.classification))
        wave_ids = tuple(_text(item, field_name="wave_id") for item in self.wave_ids)
        if len(wave_ids) != len(set(wave_ids)):
            raise ValueError("wave_ids cannot repeat.")
        object.__setattr__(self, "wave_ids", wave_ids)
        evidence_ids = tuple(_text(item, field_name="evidence_id") for item in self.evidence_ids)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_ids cannot repeat.")
        object.__setattr__(self, "evidence_ids", evidence_ids)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SegmentClassification":
        raw = dict(value)
        raw["wave_ids"] = tuple(raw.get("wave_ids", ()))
        raw["evidence_ids"] = tuple(raw.get("evidence_ids", ()))
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class CandidateScopedTechnicalEvidence(_Contract):
    evidence_id: str
    segment_id: str
    timeframe: str
    start_timestamp_utc: str
    end_timestamp_utc: str
    active_features: tuple[str, ...]
    indicator_summary: Mapping[str, Any]
    fibonacci_status: str
    duration_bars: int
    channel_status: str
    source_bundle_hash: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("evidence_id", "segment_id", "timeframe", "fibonacci_status", "channel_status"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in ("start_timestamp_utc", "end_timestamp_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        if _utc(self.end_timestamp_utc) <= _utc(self.start_timestamp_utc):
            raise ValueError("Candidate evidence interval must have positive duration.")
        features = tuple(sorted({_text(item, field_name="active_feature") for item in self.active_features}))
        object.__setattr__(self, "active_features", features)
        if not isinstance(self.indicator_summary, Mapping):
            raise TypeError("indicator_summary must be a mapping.")
        object.__setattr__(self, "indicator_summary", _freeze_json(self.indicator_summary))
        if not isinstance(self.duration_bars, int) or self.duration_bars < 1:
            raise ValueError("duration_bars must be a positive integer.")
        object.__setattr__(self, "source_bundle_hash", _hash(self.source_bundle_hash, field_name="source_bundle_hash"))
        if self.content_hash and self.content_hash != candidate_scoped_evidence_content_hash(self):
            raise ValueError("CandidateScopedTechnicalEvidence content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CandidateScopedTechnicalEvidence":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("evidence_id"):
            values["evidence_id"] = "candidate_evidence_" + canonical_sha256(
                {
                    "segment": values.get("segment_id"),
                    "timeframe": values.get("timeframe"),
                    "start": values.get("start_timestamp_utc"),
                    "end": values.get("end_timestamp_utc"),
                    "bundle": values.get("source_bundle_hash"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=candidate_scoped_evidence_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "CandidateScopedTechnicalEvidence":
        raw = dict(value)
        raw["active_features"] = tuple(raw.get("active_features", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != candidate_scoped_evidence_content_hash(result):
            raise ValueError("CandidateScopedTechnicalEvidence content_hash does not match its payload.")
        return result


def candidate_scoped_evidence_content_hash(value: CandidateScopedTechnicalEvidence | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CandidateScopedTechnicalEvidence) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class DiagonalDeclaration(_Contract):
    diagonal_type: DiagonalType
    geometry: DiagonalGeometry
    wave5_termination: DiagonalWave5Termination
    rule_policy_version: str = ADAPTIVE_DIAGONAL_RULE_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagonal_type", DiagonalType(self.diagonal_type))
        object.__setattr__(self, "geometry", DiagonalGeometry(self.geometry))
        object.__setattr__(self, "wave5_termination", DiagonalWave5Termination(self.wave5_termination))
        object.__setattr__(self, "rule_policy_version", _text(self.rule_policy_version, field_name="rule_policy_version"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DiagonalDeclaration":
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class StructuralElliottInvalidation(_Contract):
    rule: StructuralInvalidationRule
    rule_scope: str
    observed_value: float
    required_relation: str
    reference_values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule", StructuralInvalidationRule(self.rule))
        object.__setattr__(self, "rule_scope", _text(self.rule_scope, field_name="rule_scope"))
        object.__setattr__(self, "observed_value", _finite(self.observed_value, field_name="observed_value"))
        object.__setattr__(self, "required_relation", _text(self.required_relation, field_name="required_relation"))
        if not isinstance(self.reference_values, Mapping):
            raise TypeError("reference_values must be a mapping.")
        refs = {
            _text(key, field_name="reference_key"): _finite(item, field_name="reference_value")
            for key, item in self.reference_values.items()
        }
        object.__setattr__(self, "reference_values", MappingProxyType(dict(sorted(refs.items()))))


@dataclass(frozen=True, slots=True)
class CandidateBoundaryValidity(_Contract):
    candidate_id: str
    status: BoundaryValidityStatus
    direction: str
    start_timestamp_utc: str
    end_timestamp_utc: str
    start_price: float
    end_price: float
    observed_minimum: float | None
    observed_maximum: float | None
    interval_candle_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    schema_version: str = ADAPTIVE_BOUNDARY_VALIDITY_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, field_name="candidate_id"))
        object.__setattr__(self, "status", BoundaryValidityStatus(self.status))
        direction = _text(self.direction, field_name="direction").lower()
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down.")
        object.__setattr__(self, "direction", direction)
        for name in ("start_timestamp_utc", "end_timestamp_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "start_price", _finite(self.start_price, field_name="start_price"))
        object.__setattr__(self, "end_price", _finite(self.end_price, field_name="end_price"))
        for name in ("observed_minimum", "observed_maximum"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, field_name=name))
        object.__setattr__(self, "interval_candle_ids", tuple(_text(item, field_name="candle_id") for item in self.interval_candle_ids))
        object.__setattr__(self, "reason_codes", tuple(_text(item, field_name="reason_code") for item in self.reason_codes))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and self.content_hash != candidate_boundary_validity_content_hash(self):
            raise ValueError("CandidateBoundaryValidity content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CandidateBoundaryValidity":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=candidate_boundary_validity_content_hash(result))

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        verify_hash: bool = True,
    ) -> "CandidateBoundaryValidity":
        raw = dict(value)
        raw["interval_candle_ids"] = tuple(raw.get("interval_candle_ids", ()))
        raw["reason_codes"] = tuple(raw.get("reason_codes", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != candidate_boundary_validity_content_hash(result):
            raise ValueError("CandidateBoundaryValidity content_hash does not match its payload.")
        return result


def candidate_boundary_validity_content_hash(value: CandidateBoundaryValidity | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CandidateBoundaryValidity) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class RuleCalculation(_Contract):
    rule: str
    passed: bool
    observed: float
    required_relation: str
    reference: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule", _text(self.rule, field_name="rule"))
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean.")
        object.__setattr__(self, "observed", _finite(self.observed, field_name="observed"))
        object.__setattr__(self, "required_relation", _text(self.required_relation, field_name="required_relation"))
        object.__setattr__(self, "reference", _finite(self.reference, field_name="reference"))


@dataclass(frozen=True, slots=True)
class MotiveHardRuleCalculation(_Contract):
    direction: str
    family: str
    p0: float
    p1: float
    p2: float
    p3: float
    p4: float
    p5: float
    wave_1_length: float
    wave_3_length: float
    wave_5_length: float
    wave_2_origin_clearance: float
    wave_3_extreme_clearance: float
    wave_4_wave_1_overlap_amount: float
    rule_results: tuple[RuleCalculation, ...]
    structural_invalidations: tuple[StructuralElliottInvalidation, ...]
    all_hard_rules_passed: bool
    schema_version: str = ADAPTIVE_HARD_RULE_CALCULATION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        direction = _text(self.direction, field_name="direction").lower()
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down.")
        object.__setattr__(self, "direction", direction)
        family = _text(self.family, field_name="family").lower()
        if family not in {"impulse", "diagonal"}:
            raise ValueError("Motive calculation requires impulse or diagonal family.")
        object.__setattr__(self, "family", family)
        for name in (
            "p0", "p1", "p2", "p3", "p4", "p5",
            "wave_1_length", "wave_3_length", "wave_5_length",
            "wave_2_origin_clearance", "wave_3_extreme_clearance",
            "wave_4_wave_1_overlap_amount",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), field_name=name))
        object.__setattr__(self, "rule_results", tuple(self.rule_results))
        object.__setattr__(self, "structural_invalidations", tuple(self.structural_invalidations))
        if not isinstance(self.all_hard_rules_passed, bool):
            raise TypeError("all_hard_rules_passed must be boolean.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and self.content_hash != motive_hard_rule_calculation_content_hash(self):
            raise ValueError("MotiveHardRuleCalculation content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "MotiveHardRuleCalculation":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=motive_hard_rule_calculation_content_hash(result))


def motive_hard_rule_calculation_content_hash(value: MotiveHardRuleCalculation | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, MotiveHardRuleCalculation) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class DiagonalGeometryCalculation(_Contract):
    declaration: DiagonalDeclaration
    diagonal_type: DiagonalType
    internal_structure_policy: str
    expected_child_structure: tuple[str, ...]
    observed_child_structure: tuple[str, ...]
    policy_source: str
    p0: float
    p1: float
    p2: float
    p3: float
    p4: float
    p5: float
    actionary_boundary_slope: float
    corrective_boundary_slope: float
    initial_boundary_width: float
    terminal_boundary_width: float
    convergence_or_divergence: str
    projected_actionary_boundary_at_wave_5: float
    projected_corrective_boundary_at_wave_5: float
    wave_5_boundary_distance: float
    observed_wave_5_termination: DiagonalWave5Termination
    child_family_rules_passed: bool
    wave_4_overlap_passed: bool
    geometry_passed: bool
    wave_5_termination_passed: bool
    all_hard_rules_passed: bool
    rule_results: tuple[RuleCalculation, ...]
    structural_invalidations: tuple[StructuralElliottInvalidation, ...]
    policy_version: str = ADAPTIVE_DIAGONAL_RULE_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.declaration, DiagonalDeclaration):
            raise TypeError("declaration must be DiagonalDeclaration.")
        object.__setattr__(self, "diagonal_type", DiagonalType(self.diagonal_type))
        if self.diagonal_type is not self.declaration.diagonal_type:
            raise ValueError("diagonal_type must match the diagonal declaration.")
        object.__setattr__(
            self,
            "internal_structure_policy",
            _text(self.internal_structure_policy, field_name="internal_structure_policy"),
        )
        if self.internal_structure_policy != ADAPTIVE_DIAGONAL_INTERNAL_FAMILY_POLICY_VERSION:
            raise ValueError("Unsupported diagonal internal-family policy.")
        expected = tuple(
            _text(item, field_name="expected_child_structure")
            for item in self.expected_child_structure
        )
        observed = tuple(
            _text(item, field_name="observed_child_structure")
            for item in self.observed_child_structure
        )
        if len(expected) != 5 or len(observed) != 5:
            raise ValueError("Diagonal internal structure requires exactly five child entries.")
        object.__setattr__(self, "expected_child_structure", expected)
        object.__setattr__(self, "observed_child_structure", observed)
        object.__setattr__(
            self,
            "policy_source",
            _text(self.policy_source, field_name="policy_source"),
        )
        for name in (
            "p0", "p1", "p2", "p3", "p4", "p5",
            "actionary_boundary_slope", "corrective_boundary_slope",
            "initial_boundary_width", "terminal_boundary_width",
            "projected_actionary_boundary_at_wave_5",
            "projected_corrective_boundary_at_wave_5", "wave_5_boundary_distance",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), field_name=name))
        convergence = _text(self.convergence_or_divergence, field_name="convergence_or_divergence")
        if convergence not in {"converging", "diverging", "parallel_or_ambiguous"}:
            raise ValueError("convergence_or_divergence is invalid.")
        object.__setattr__(self, "convergence_or_divergence", convergence)
        object.__setattr__(self, "observed_wave_5_termination", DiagonalWave5Termination(self.observed_wave_5_termination))
        for name in (
            "child_family_rules_passed", "wave_4_overlap_passed", "geometry_passed",
            "wave_5_termination_passed", "all_hard_rules_passed",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        object.__setattr__(self, "rule_results", tuple(self.rule_results))
        object.__setattr__(self, "structural_invalidations", tuple(self.structural_invalidations))
        object.__setattr__(self, "policy_version", _text(self.policy_version, field_name="policy_version"))
        if self.content_hash and self.content_hash != diagonal_geometry_calculation_content_hash(self):
            raise ValueError("DiagonalGeometryCalculation content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "DiagonalGeometryCalculation":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=diagonal_geometry_calculation_content_hash(result))


def diagonal_geometry_calculation_content_hash(value: DiagonalGeometryCalculation | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, DiagonalGeometryCalculation) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def diagonal_internal_family_policy(
    diagonal_type: DiagonalType | str,
) -> tuple[tuple[frozenset[str], ...], tuple[str, ...]]:
    """Return the active, versioned family template for a diagonal."""

    kind = DiagonalType(diagonal_type)
    if kind is DiagonalType.LEADING:
        return (
            (
                frozenset({"impulse", "diagonal"}),
                frozenset({"zigzag"}),
                frozenset({"impulse", "diagonal"}),
                frozenset({"zigzag"}),
                frozenset({"impulse", "diagonal"}),
            ),
            ("motive_five", "zigzag_three", "motive_five", "zigzag_three", "motive_five"),
        )
    return (
        tuple(frozenset({"zigzag"}) for _ in range(5)),
        tuple("zigzag_three" for _ in range(5)),
    )


@dataclass(frozen=True, slots=True)
class ParentStructuralAssessment(_Contract):
    parent_wave_id: str
    parent_hard_rule_failed: bool
    structural_impossibility_proven: bool
    failed_parent_rules: tuple[str, ...]
    child_attempts_examined: int
    admissible_family_space: tuple[str, ...]
    examined_family_space: tuple[str, ...]
    deterministically_eliminated_families: tuple[str, ...]
    remaining_admissible_reconstructions: tuple[str, ...]
    evidence_complete: bool
    unresolved_boundary_reselection: bool
    missing_material_timeframe: bool
    parent_family_reclassification_required: bool
    family_exhaustive_impossibility_supported: bool
    calculation_hashes: tuple[str, ...]
    impossibility_policy_version: str = ADAPTIVE_PARENT_IMPOSSIBILITY_POLICY_VERSION
    schema_version: str = ADAPTIVE_PARENT_STRUCTURAL_ASSESSMENT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_wave_id", _text(self.parent_wave_id, field_name="parent_wave_id"))
        for name in (
            "parent_hard_rule_failed",
            "structural_impossibility_proven",
            "evidence_complete",
            "unresolved_boundary_reselection",
            "missing_material_timeframe",
            "parent_family_reclassification_required",
            "family_exhaustive_impossibility_supported",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        if not isinstance(self.child_attempts_examined, int) or self.child_attempts_examined < 0:
            raise ValueError("child_attempts_examined must be a non-negative integer.")
        for name in (
            "failed_parent_rules",
            "admissible_family_space",
            "examined_family_space",
            "deterministically_eliminated_families",
            "remaining_admissible_reconstructions",
        ):
            values = tuple(_text(item, field_name=name) for item in getattr(self, name))
            if len(values) != len(set(values)):
                raise ValueError(f"{name} cannot contain duplicates.")
            object.__setattr__(self, name, values)
        family_space = set(self.admissible_family_space)
        if not set(self.examined_family_space).issubset(family_space):
            raise ValueError("examined_family_space must be a subset of admissible_family_space.")
        if not set(self.deterministically_eliminated_families).issubset(family_space):
            raise ValueError("Eliminated families must be admissible parent families.")
        expected_remaining = tuple(
            item
            for item in self.admissible_family_space
            if item not in set(self.deterministically_eliminated_families)
        )
        if self.remaining_admissible_reconstructions != expected_remaining:
            raise ValueError("remaining_admissible_reconstructions is not deterministic.")
        hashes = tuple(_hash(item, field_name="calculation_hash") for item in self.calculation_hashes)
        if len(hashes) != len(set(hashes)):
            raise ValueError("calculation_hashes cannot contain duplicates.")
        object.__setattr__(self, "calculation_hashes", hashes)
        if self.parent_hard_rule_failed != bool(self.failed_parent_rules):
            raise ValueError("parent_hard_rule_failed must be derived from failed_parent_rules.")
        if self.family_exhaustive_impossibility_supported:
            raise ValueError(
                "This policy version does not implement exhaustive family/segmentation proof."
            )
        if self.deterministically_eliminated_families:
            raise ValueError(
                "Families cannot be marked eliminated while exhaustive impossibility is unsupported."
            )
        if self.structural_impossibility_proven != self.parent_hard_rule_failed:
            raise ValueError(
                "Direct frozen-parent hard-rule failure is the only supported impossibility proof."
            )
        object.__setattr__(
            self,
            "impossibility_policy_version",
            _text(
                self.impossibility_policy_version,
                field_name="impossibility_policy_version",
            ),
        )
        if (
            self.impossibility_policy_version
            != ADAPTIVE_PARENT_IMPOSSIBILITY_POLICY_VERSION
        ):
            raise ValueError("Unsupported parent impossibility policy version.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and self.content_hash != parent_structural_assessment_content_hash(self):
            raise ValueError("ParentStructuralAssessment content_hash does not match its payload.")

    @classmethod
    def assess(
        cls,
        *,
        parent_wave_id: str,
        failed_parent_rules: Sequence[str] = (),
        child_attempts_examined: int = 0,
        admissible_family_space: Sequence[str],
        examined_family_space: Sequence[str] = (),
        deterministically_eliminated_families: Sequence[str] = (),
        evidence_complete: bool = False,
        unresolved_boundary_reselection: bool = False,
        missing_material_timeframe: bool = False,
        parent_family_reclassification_required: bool = False,
        calculation_hashes: Sequence[str] = (),
    ) -> "ParentStructuralAssessment":
        families = tuple(dict.fromkeys(str(item).strip().lower() for item in admissible_family_space if str(item).strip()))
        examined = tuple(dict.fromkeys(str(item).strip().lower() for item in examined_family_space if str(item).strip()))
        requested_eliminations = tuple(
            dict.fromkeys(
                str(value).strip().lower()
                for value in deterministically_eliminated_families
                if str(value).strip()
            )
        )
        if requested_eliminations:
            raise ValueError(
                "Exhaustive family elimination is not supported by the active policy."
            )
        eliminated: tuple[str, ...] = ()
        failed = tuple(dict.fromkeys(str(item).strip() for item in failed_parent_rules if str(item).strip()))
        remaining = tuple(item for item in families if item not in set(eliminated))
        impossible = bool(failed)
        result = cls(
            parent_wave_id=parent_wave_id,
            parent_hard_rule_failed=bool(failed),
            structural_impossibility_proven=impossible,
            failed_parent_rules=failed,
            child_attempts_examined=child_attempts_examined,
            admissible_family_space=families,
            examined_family_space=examined,
            deterministically_eliminated_families=eliminated,
            remaining_admissible_reconstructions=remaining,
            evidence_complete=evidence_complete,
            unresolved_boundary_reselection=unresolved_boundary_reselection,
            missing_material_timeframe=missing_material_timeframe,
            parent_family_reclassification_required=(
                parent_family_reclassification_required
            ),
            family_exhaustive_impossibility_supported=(
                FAMILY_EXHAUSTIVE_IMPOSSIBILITY_SUPPORTED
            ),
            calculation_hashes=tuple(calculation_hashes),
        )
        return replace(result, content_hash=parent_structural_assessment_content_hash(result))


def parent_structural_assessment_content_hash(
    value: ParentStructuralAssessment | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ParentStructuralAssessment) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ChildReconstructionDecision(_Contract):
    parent_wave_id: str
    attempts_used: int
    maximum_attempts: int
    parent_status: AdaptiveParentProofDisposition
    search_outcome: AdaptiveSearchOutcome
    attempt_dispositions: tuple[AdaptiveChildAttemptDisposition, ...]
    reason_codes: tuple[str, ...]
    parent_assessment: ParentStructuralAssessment | None = None
    policy_version: str = ADAPTIVE_CHILD_RECONSTRUCTION_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_wave_id", _text(self.parent_wave_id, field_name="parent_wave_id"))
        if not isinstance(self.attempts_used, int) or self.attempts_used < 0:
            raise ValueError("attempts_used must be a non-negative integer.")
        if not isinstance(self.maximum_attempts, int) or self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive.")
        if self.attempts_used > self.maximum_attempts:
            raise ValueError("attempts_used cannot exceed maximum_attempts.")
        object.__setattr__(self, "parent_status", AdaptiveParentProofDisposition(self.parent_status))
        object.__setattr__(self, "search_outcome", AdaptiveSearchOutcome(self.search_outcome))
        object.__setattr__(self, "attempt_dispositions", tuple(AdaptiveChildAttemptDisposition(item) for item in self.attempt_dispositions))
        object.__setattr__(self, "reason_codes", tuple(_text(item, field_name="reason_code") for item in self.reason_codes))
        if self.parent_assessment is not None and not isinstance(
            self.parent_assessment, ParentStructuralAssessment
        ):
            raise TypeError("parent_assessment must be ParentStructuralAssessment or null.")
        object.__setattr__(self, "policy_version", _text(self.policy_version, field_name="policy_version"))

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.parent_assessment is None:
            payload.pop("parent_assessment", None)
        return payload


def same_degree_parent_child_forbidden(parent_degree: str, child_degree: str) -> bool:
    """Return true when a direct Elliott parent and child use one degree."""

    return parent_degree == child_degree


def validate_direct_child_degree(parent_degree: str, child_degree: str) -> tuple[str, ...]:
    if parent_degree not in STANDARD_DEGREES or parent_degree == "Unassigned":
        return ("parent_degree_invalid",)
    if child_degree not in STANDARD_DEGREES or child_degree == "Unassigned":
        return ("child_degree_invalid",)
    if same_degree_parent_child_forbidden(parent_degree, child_degree):
        return ("same_degree_parent_child_forbidden",)
    parent_index = STANDARD_DEGREES.index(parent_degree)
    child_index = STANDARD_DEGREES.index(child_degree)
    if child_index != parent_index + 1:
        return ("direct_child_degree_descent_invalid",)
    return ()


def calculate_motive_hard_rules(
    points: Sequence[float],
    *,
    direction: str,
    family: str,
) -> MotiveHardRuleCalculation:
    if len(points) != 6:
        raise ValueError("Motive hard-rule calculation requires p0 through p5.")
    p0, p1, p2, p3, p4, p5 = (
        _finite(value, field_name=f"p{index}") for index, value in enumerate(points)
    )
    direction = _text(direction, field_name="direction").lower()
    family = _text(family, field_name="family").lower()
    if direction not in {"up", "down"}:
        raise ValueError("direction must be up or down.")
    if family not in {"impulse", "diagonal"}:
        raise ValueError("family must be impulse or diagonal.")

    sign = 1.0 if direction == "up" else -1.0
    wave_1_length = sign * (p1 - p0)
    wave_3_length = sign * (p3 - p2)
    wave_5_length = sign * (p5 - p4)
    wave_2_clearance = sign * (p2 - p0)
    wave_3_clearance = sign * (p3 - p1)
    overlap_amount = max(0.0, sign * (p1 - p4))

    wave2_pass = wave_2_clearance >= -_EPSILON
    wave3_extreme_pass = wave_3_clearance > _EPSILON
    positive_actionary = min(wave_1_length, wave_3_length, wave_5_length) > _EPSILON
    wave3_shortest = (
        wave_3_length + _EPSILON < wave_1_length
        and wave_3_length + _EPSILON < wave_5_length
    )
    wave3_length_pass = positive_actionary and not wave3_shortest
    if family == "impulse":
        wave4_pass = sign * (p4 - p1) > _EPSILON
        wave4_rule = StructuralInvalidationRule.STANDARD_IMPULSE_WAVE_4_OVERLAPS_WAVE_1
        wave4_relation = "overlap_amount == 0 and wave_4 clears wave_1 extreme"
    else:
        wave4_pass = overlap_amount > _EPSILON
        wave4_rule = StructuralInvalidationRule.DIAGONAL_WAVE_4_OVERLAP_MISSING
        wave4_relation = "overlap_amount > 0"

    results = (
        RuleCalculation(
            rule=StructuralInvalidationRule.WAVE_2_BEYOND_WAVE_1_ORIGIN.value,
            passed=wave2_pass,
            observed=wave_2_clearance,
            required_relation=">= 0",
            reference=0.0,
        ),
        RuleCalculation(
            rule=StructuralInvalidationRule.WAVE_3_FAILED_TO_EXCEED_WAVE_1_EXTREME.value,
            passed=wave3_extreme_pass,
            observed=wave_3_clearance,
            required_relation="> 0",
            reference=0.0,
        ),
        RuleCalculation(
            rule=StructuralInvalidationRule.WAVE_3_IS_SHORTEST.value,
            passed=wave3_length_pass,
            observed=wave_3_length,
            required_relation=">= min(wave_1_length, wave_5_length)",
            reference=min(wave_1_length, wave_5_length),
        ),
        RuleCalculation(
            rule=wave4_rule.value,
            passed=wave4_pass,
            observed=overlap_amount,
            required_relation=wave4_relation,
            reference=0.0,
        ),
    )
    invalidations: list[StructuralElliottInvalidation] = []
    for result in results:
        if result.passed:
            continue
        invalidations.append(
            StructuralElliottInvalidation(
                rule=StructuralInvalidationRule(result.rule),
                rule_scope="motive_parent",
                observed_value=result.observed,
                required_relation=result.required_relation,
                reference_values={
                    "reference": result.reference,
                    "p0": p0,
                    "p1": p1,
                    "p2": p2,
                    "p3": p3,
                    "p4": p4,
                    "p5": p5,
                },
            )
        )
    return MotiveHardRuleCalculation.create(
        direction=direction,
        family=family,
        p0=p0,
        p1=p1,
        p2=p2,
        p3=p3,
        p4=p4,
        p5=p5,
        wave_1_length=wave_1_length,
        wave_3_length=wave_3_length,
        wave_5_length=wave_5_length,
        wave_2_origin_clearance=wave_2_clearance,
        wave_3_extreme_clearance=wave_3_clearance,
        wave_4_wave_1_overlap_amount=overlap_amount,
        rule_results=results,
        structural_invalidations=tuple(invalidations),
        all_hard_rules_passed=all(item.passed for item in results),
    )


def _time_axis(timestamps: Sequence[str]) -> tuple[float, ...]:
    if len(timestamps) != 6:
        raise ValueError("Diagonal calculation requires six timestamps.")
    parsed = tuple(_utc_text(item, field_name="timestamp") for item in timestamps)
    seconds = tuple((_utc(item) - _utc(parsed[0])).total_seconds() for item in parsed)
    if any(right <= left for left, right in zip(seconds, seconds[1:])):
        raise ValueError("Diagonal timestamps must be strictly increasing.")
    return seconds


def _line(x0: float, y0: float, x1: float, y1: float, x: float) -> tuple[float, float]:
    if x1 <= x0:
        raise ValueError("Trend boundary requires increasing timestamps.")
    slope = (y1 - y0) / (x1 - x0)
    return y0 + slope * (x - x0), slope


def calculate_diagonal_geometry(
    points: Sequence[float],
    timestamps: Sequence[str],
    *,
    direction: str,
    declaration: DiagonalDeclaration,
    child_families: Sequence[str],
) -> DiagonalGeometryCalculation:
    if not isinstance(declaration, DiagonalDeclaration):
        raise TypeError("declaration must be DiagonalDeclaration.")
    motive = calculate_motive_hard_rules(points, direction=direction, family="diagonal")
    p0, p1, p2, p3, p4, p5 = (motive.p0, motive.p1, motive.p2, motive.p3, motive.p4, motive.p5)
    x0, x1, x2, x3, x4, x5 = _time_axis(timestamps)
    action_at_x2, action_slope = _line(x1, p1, x3, p3, x2)
    action_at_x5, _ = _line(x1, p1, x3, p3, x5)
    corrective_at_x2, corrective_slope = _line(x2, p2, x4, p4, x2)
    corrective_at_x5, _ = _line(x2, p2, x4, p4, x5)
    sign = 1.0 if motive.direction == "up" else -1.0
    initial_width = sign * (action_at_x2 - corrective_at_x2)
    terminal_width = sign * (action_at_x5 - corrective_at_x5)
    width_delta = terminal_width - initial_width
    width_tolerance = max(abs(initial_width), 1.0) * 1e-9
    convergence = (
        "converging"
        if width_delta < -width_tolerance
        else "diverging"
        if width_delta > width_tolerance
        else "parallel_or_ambiguous"
    )
    expected_convergence = (
        "converging"
        if declaration.geometry is DiagonalGeometry.CONTRACTING
        else "diverging"
    )
    geometry_pass = (
        initial_width > _EPSILON
        and terminal_width > _EPSILON
        and convergence == expected_convergence
    )

    normalized_families = tuple(str(item).strip().lower() for item in child_families)
    permitted_families, expected_structure = diagonal_internal_family_policy(
        declaration.diagonal_type
    )
    family_pass = len(normalized_families) == 5 and all(
        observed in permitted
        for observed, permitted in zip(normalized_families, permitted_families)
    )

    wave5_boundary_distance = sign * (p5 - action_at_x5)
    boundary_tolerance = max(abs(motive.wave_1_length) * 0.02, 1e-8)
    truncated = sign * (p5 - p3) <= _EPSILON
    if truncated:
        observed_termination = DiagonalWave5Termination.TRUNCATED
    elif wave5_boundary_distance > boundary_tolerance:
        observed_termination = DiagonalWave5Termination.THROW_OVER
    else:
        observed_termination = DiagonalWave5Termination.NORMAL
    termination_pass = observed_termination is declaration.wave5_termination
    corrective_side_pass = sign * (p5 - corrective_at_x5) > _EPSILON
    geometry_pass = geometry_pass and corrective_side_pass

    rule_results = (
        RuleCalculation(
            rule=StructuralInvalidationRule.DIAGONAL_CHILD_FAMILY_MISMATCH.value,
            passed=family_pass,
            observed=1.0 if family_pass else 0.0,
            required_relation=(
                "leading motive-five/zigzag/motive-five/zigzag/motive-five"
                if declaration.diagonal_type is DiagonalType.LEADING
                else "ending diagonal: every leg is a zigzag-family three"
            ),
            reference=1.0,
        ),
        RuleCalculation(
            rule=StructuralInvalidationRule.DIAGONAL_WAVE_4_OVERLAP_MISSING.value,
            passed=motive.wave_4_wave_1_overlap_amount > _EPSILON,
            observed=motive.wave_4_wave_1_overlap_amount,
            required_relation="> 0",
            reference=0.0,
        ),
        RuleCalculation(
            rule=StructuralInvalidationRule.DIAGONAL_GEOMETRY_MISMATCH.value,
            passed=geometry_pass,
            observed=terminal_width,
            required_relation=(
                "terminal boundary width < initial boundary width"
                if declaration.geometry is DiagonalGeometry.CONTRACTING
                else "terminal boundary width > initial boundary width"
            ),
            reference=initial_width,
        ),
        RuleCalculation(
            rule=StructuralInvalidationRule.DIAGONAL_WAVE_5_TERMINATION_MISMATCH.value,
            passed=termination_pass,
            observed=wave5_boundary_distance,
            required_relation=f"declared {declaration.wave5_termination.value}",
            reference=0.0,
        ),
    )
    invalidations = list(motive.structural_invalidations)
    for result in rule_results:
        if result.passed:
            continue
        invalidations.append(
            StructuralElliottInvalidation(
                rule=StructuralInvalidationRule(result.rule),
                rule_scope="diagonal_parent",
                observed_value=result.observed,
                required_relation=result.required_relation,
                reference_values={
                    "reference": result.reference,
                    "initial_boundary_width": initial_width,
                    "terminal_boundary_width": terminal_width,
                    "actionary_boundary_at_wave_5": action_at_x5,
                    "corrective_boundary_at_wave_5": corrective_at_x5,
                    "p5": p5,
                },
            )
        )
    all_pass = motive.all_hard_rules_passed and all(item.passed for item in rule_results)
    return DiagonalGeometryCalculation.create(
        declaration=declaration,
        diagonal_type=declaration.diagonal_type,
        internal_structure_policy=ADAPTIVE_DIAGONAL_INTERNAL_FAMILY_POLICY_VERSION,
        expected_child_structure=expected_structure,
        observed_child_structure=normalized_families,
        policy_source=ADAPTIVE_DIAGONAL_POLICY_SOURCE,
        p0=p0,
        p1=p1,
        p2=p2,
        p3=p3,
        p4=p4,
        p5=p5,
        actionary_boundary_slope=action_slope,
        corrective_boundary_slope=corrective_slope,
        initial_boundary_width=initial_width,
        terminal_boundary_width=terminal_width,
        convergence_or_divergence=convergence,
        projected_actionary_boundary_at_wave_5=action_at_x5,
        projected_corrective_boundary_at_wave_5=corrective_at_x5,
        wave_5_boundary_distance=wave5_boundary_distance,
        observed_wave_5_termination=observed_termination,
        child_family_rules_passed=family_pass,
        wave_4_overlap_passed=motive.wave_4_wave_1_overlap_amount > _EPSILON,
        geometry_passed=geometry_pass,
        wave_5_termination_passed=termination_pass,
        all_hard_rules_passed=all_pass,
        rule_results=rule_results,
        structural_invalidations=tuple(invalidations),
    )


def evaluate_candidate_boundary(
    *,
    candidate_id: str,
    direction: str,
    start_timestamp_utc: str,
    end_timestamp_utc: str,
    start_price: float,
    end_price: float,
    candles: Sequence[Any],
) -> CandidateBoundaryValidity:
    start = _utc(_utc_text(start_timestamp_utc, field_name="start_timestamp_utc"))
    end = _utc(_utc_text(end_timestamp_utc, field_name="end_timestamp_utc"))
    direction = _text(direction, field_name="direction").lower()
    start_price = _finite(start_price, field_name="start_price")
    end_price = _finite(end_price, field_name="end_price")
    rows = [
        item
        for item in candles
        if start <= _utc(str(getattr(item, "timestamp_utc"))) <= end
    ]
    if not rows:
        return CandidateBoundaryValidity.create(
            candidate_id=candidate_id,
            status=BoundaryValidityStatus.CANDIDATE_SEGMENTATION_FAILED,
            direction=direction,
            start_timestamp_utc=start.isoformat(timespec="seconds"),
            end_timestamp_utc=end.isoformat(timespec="seconds"),
            start_price=start_price,
            end_price=end_price,
            observed_minimum=None,
            observed_maximum=None,
            interval_candle_ids=(),
            reason_codes=(BoundaryValidityStatus.CANDIDATE_SEGMENTATION_FAILED.value,),
        )
    observed_minimum = min(_finite(getattr(item, "low"), field_name="low") for item in rows)
    observed_maximum = max(_finite(getattr(item, "high"), field_name="high") for item in rows)
    tolerance = max(abs(start_price), abs(end_price), 1.0) * 1e-9
    if direction == "up":
        start_failed = observed_minimum < start_price - tolerance
        end_failed = observed_maximum > end_price + tolerance
    elif direction == "down":
        start_failed = observed_maximum > start_price + tolerance
        end_failed = observed_minimum < end_price - tolerance
    else:
        raise ValueError("direction must be up or down.")
    reasons: list[str] = []
    if start_failed or end_failed:
        reasons.append(BoundaryValidityStatus.PIVOT_NOT_TRUE_EXTREME.value)
        reasons.append(BoundaryValidityStatus.BOUNDARY_REQUIRES_RESELECTION.value)
        status = BoundaryValidityStatus.PIVOT_NOT_TRUE_EXTREME
    else:
        status = BoundaryValidityStatus.VALID
    return CandidateBoundaryValidity.create(
        candidate_id=candidate_id,
        status=status,
        direction=direction,
        start_timestamp_utc=start.isoformat(timespec="seconds"),
        end_timestamp_utc=end.isoformat(timespec="seconds"),
        start_price=start_price,
        end_price=end_price,
        observed_minimum=observed_minimum,
        observed_maximum=observed_maximum,
        interval_candle_ids=tuple(str(getattr(item, "candle_id")) for item in rows),
        reason_codes=tuple(reasons),
    )


def summarize_child_reconstruction(
    *,
    parent_wave_id: str,
    attempt_dispositions: Sequence[AdaptiveChildAttemptDisposition | str],
    maximum_attempts: int = MAX_CHILD_RECOUNTS_PER_PARENT,
    parent_assessment: ParentStructuralAssessment | None = None,
) -> ChildReconstructionDecision:
    attempts = tuple(AdaptiveChildAttemptDisposition(item) for item in attempt_dispositions)
    if len(attempts) > maximum_attempts:
        raise ValueError("Attempt count exceeds the bounded reconstruction policy.")
    if parent_assessment is not None:
        if parent_assessment.parent_wave_id != parent_wave_id:
            raise ValueError("Parent assessment belongs to another wave.")
        parent_hard_rule_failed = parent_assessment.parent_hard_rule_failed
        parent_family_reclassification_required = (
            parent_assessment.parent_family_reclassification_required
        )
    else:
        # Callers without enough immutable parent evidence stay conservative.
        parent_hard_rule_failed = False
        parent_family_reclassification_required = False
    reasons: list[str] = []
    if parent_hard_rule_failed:
        parent = AdaptiveParentProofDisposition.INCONSISTENT
        search = AdaptiveSearchOutcome.PARENT_STRUCTURALLY_INCONSISTENT
        reasons.append("parent_hard_rule_failed")
    elif any(item is AdaptiveChildAttemptDisposition.VERIFIED for item in attempts):
        parent = AdaptiveParentProofDisposition.VERIFIED
        search = AdaptiveSearchOutcome.VERIFIED
    elif parent_family_reclassification_required or any(
        item
        is AdaptiveChildAttemptDisposition.PARENT_FAMILY_RECLASSIFICATION_REQUIRED
        for item in attempts
    ):
        parent = AdaptiveParentProofDisposition.UNPROVEN
        search = AdaptiveSearchOutcome.RECOUNT_REQUIRED
        reasons.append("parent_family_reclassification_required")
    elif attempts and all(item is AdaptiveChildAttemptDisposition.NOT_COVERED for item in attempts):
        parent = AdaptiveParentProofDisposition.NOT_COVERED
        search = AdaptiveSearchOutcome.NOT_COVERED
        reasons.append("all_attempts_not_covered")
    elif len(attempts) >= maximum_attempts:
        parent = AdaptiveParentProofDisposition.UNPROVEN
        search = AdaptiveSearchOutcome.CANDIDATE_SEARCH_EXHAUSTED
        reasons.append("bounded_search_exhausted_without_impossibility_proof")
    elif any(
        item
        in {
            AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED,
            AdaptiveChildAttemptDisposition.BOUNDARY_REQUIRES_RESELECTION,
            AdaptiveChildAttemptDisposition.PIVOT_NOT_TRUE_EXTREME,
            AdaptiveChildAttemptDisposition.CANDIDATE_SEGMENTATION_FAILED,
            AdaptiveChildAttemptDisposition.DUPLICATE_RECONSTRUCTION_CANDIDATE,
        }
        for item in attempts
    ):
        parent = AdaptiveParentProofDisposition.UNPROVEN
        search = AdaptiveSearchOutcome.RECOUNT_REQUIRED
        reasons.append("child_candidate_failed_parent_remains_eligible")
    else:
        parent = AdaptiveParentProofDisposition.UNPROVEN
        search = AdaptiveSearchOutcome.UNPROVEN
    return ChildReconstructionDecision(
        parent_wave_id=parent_wave_id,
        attempts_used=len(attempts),
        maximum_attempts=maximum_attempts,
        parent_status=parent,
        search_outcome=search,
        attempt_dispositions=attempts,
        reason_codes=tuple(reasons),
        parent_assessment=parent_assessment,
    )


__all__ = [
    "ADAPTIVE_ANALYSIS_SCOPE_SCHEMA_VERSION",
    "ADAPTIVE_BOUNDARY_VALIDITY_SCHEMA_VERSION",
    "ADAPTIVE_CHILD_RECONSTRUCTION_POLICY_VERSION",
    "ADAPTIVE_DIAGONAL_INTERNAL_FAMILY_POLICY_VERSION",
    "ADAPTIVE_DIAGONAL_POLICY_SOURCE",
    "ADAPTIVE_DIAGONAL_RULE_POLICY_VERSION",
    "ADAPTIVE_HARD_RULE_CALCULATION_SCHEMA_VERSION",
    "ADAPTIVE_GROUP_CLASSIFICATION_SCHEMA_VERSION",
    "ADAPTIVE_PARENT_STRUCTURAL_ASSESSMENT_SCHEMA_VERSION",
    "ADAPTIVE_PARENT_IMPOSSIBILITY_POLICY_VERSION",
    "ADAPTIVE_PRICE_SEGMENTATION_LEGACY_SCHEMA_VERSION",
    "ADAPTIVE_PRICE_SEGMENTATION_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_STRUCTURAL_GROUPING_SCHEMA_VERSION",
    "ADAPTIVE_SEGMENT_CLASSIFICATION_SCHEMA_VERSION",
    "MAX_CHILD_CANDIDATES_PER_PARENT",
    "MAX_CHILD_RECOUNTS_PER_PARENT",
    "FAMILY_EXHAUSTIVE_IMPOSSIBILITY_SUPPORTED",
    "AdaptiveAnalysisScope",
    "AdaptiveChildAttemptDisposition",
    "AdaptiveParentProofDisposition",
    "AdaptiveSearchOutcome",
    "AnalysisBoundaryKind",
    "AnalysisScopeBoundary",
    "BoundaryValidityStatus",
    "CandidateBoundaryValidity",
    "CandidateScopedTechnicalEvidence",
    "ChildReconstructionDecision",
    "DiagonalDeclaration",
    "DiagonalGeometry",
    "DiagonalGeometryCalculation",
    "DiagonalType",
    "DiagonalWave5Termination",
    "GroupClassification",
    "MotiveHardRuleCalculation",
    "ParentStructuralAssessment",
    "PriceSegmentCandidate",
    "PriceSegmentKind",
    "PriceSegmentationHypothesis",
    "PriceExtremumType",
    "RootStructuralGroupingHypothesis",
    "RuleCalculation",
    "SegmentClassification",
    "SegmentClassificationKind",
    "SegmentCompletionState",
    "StructuralElliottInvalidation",
    "StructuralSegmentGroup",
    "StructuralInvalidationRule",
    "adaptive_analysis_scope_content_hash",
    "calculate_diagonal_geometry",
    "calculate_motive_hard_rules",
    "candidate_boundary_validity_content_hash",
    "candidate_scoped_evidence_content_hash",
    "diagonal_geometry_calculation_content_hash",
    "diagonal_internal_family_policy",
    "evaluate_candidate_boundary",
    "motive_hard_rule_calculation_content_hash",
    "parent_structural_assessment_content_hash",
    "price_segmentation_content_hash",
    "root_structural_grouping_content_hash",
    "same_degree_parent_child_forbidden",
    "summarize_child_reconstruction",
    "validate_direct_child_degree",
]
