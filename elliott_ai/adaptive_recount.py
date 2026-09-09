"""Adaptive, blind, pre-freeze Elliott recount coordination.

This module coordinates existing catalog-pivot candidate and deterministic
verification components.  It does not fetch market data, call a provider
without explicit authorization, write SQLite, or create a Phase 11 forecast.
The authoritative technical freeze remains ``ElliottAgent.resolve_degrees``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .adaptive_market_data import (
    BundleValidationResult,
    BundleValidationStatus,
    DataRequestManifest,
    NativeOHLCVBundle,
    manifest_from_planner,
    validate_bundle_for_manifest,
)
from .adaptive_structure import (
    MAX_CHILD_CANDIDATES_PER_PARENT,
    MAX_CHILD_RECOUNTS_PER_PARENT,
    AdaptiveAnalysisScope,
    AdaptiveChildAttemptDisposition,
    AdaptiveParentProofDisposition,
    AdaptiveSearchOutcome,
    AnalysisBoundaryKind,
    AnalysisScopeBoundary,
    BoundaryValidityStatus,
    CandidateBoundaryValidity,
    CandidateScopedTechnicalEvidence,
    ChildReconstructionDecision,
    DiagonalDeclaration,
    GroupClassification,
    ParentStructuralAssessment,
    PriceExtremumType,
    PriceSegmentCandidate,
    PriceSegmentKind,
    PriceSegmentationHypothesis,
    RootStructuralGroupingHypothesis,
    SegmentClassification,
    SegmentClassificationKind,
    SegmentCompletionState,
    StructuralSegmentGroup,
    calculate_diagonal_geometry,
    calculate_motive_hard_rules,
    evaluate_candidate_boundary,
    summarize_child_reconstruction,
    validate_direct_child_degree,
)
from .adaptive_timeframe_planner import (
    AdaptiveTimeframeDecision,
    AdaptiveTimeframePlanner,
    AdaptiveTimeframeRequest,
    ROOT_TIMEFRAME_PLANNER_POLICY_VERSION,
    StructuralQuestion,
    TimeframeEvidenceCoverage,
    TimeframePlanningReason,
    TimeframePlanningStatus,
)
from .blind_candidate_rules import (
    BlindCandidateRulesPack,
    blind_candidate_rules_pack_content_hash,
    default_blind_candidate_rules_pack,
)
from .correction_semantics import CORRECTIVE_FAMILIES, MOTIVE_FAMILIES
from .forecast_records import (
    DatasetCutoff,
    DatasetHashScope,
    canonical_forecast_json,
    canonical_sha256,
)
from .indicators import enrich_candles, features_for_profile, summarize_indicators
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGenerationError,
    CandidateGenerationFailureCode,
    CandidateGenerationPolicy,
    CandidateGraphRole,
    CandidateInvalidation,
    CandidateProofScope,
    ChildReconstructionContext,
    GraphGenerationProvider,
    LowerTimeframeCandidateGenerationRequest,
    LowerTimeframeCandidateGenerator,
    NativeOHLCVScope,
    NativeOHLCVWindow,
    ParentCandidateSnapshot,
    RECONSTRUCTION_ALLOWED_CHANGES,
    ReconstructionExcludedGraph,
    TypedPivot,
    WindowCoverageRole,
    build_native_pivot_catalog,
    candidate_invalidation_openai_json_schema,
    diagonal_declaration_openai_json_schema,
    expected_child_positions,
    graph_signature,
    next_standard_degree,
    optional_candidate_invalidation_openai_json_schema,
)
from .lower_timeframe_subdivision_verifier import (
    LowerTimeframeSubdivisionVerifier,
    SubdivisionVerifierPolicy,
    SubdivisionVerifierResult,
    SubdivisionVerificationStatus,
    VerificationReasonCode,
    build_subdivision_verification_request,
)
from .pivot_chronology import (
    ChronologyExtremum,
    IntrabarOrderStatus,
    PivotChronologyGroup,
    PivotChronologyMember,
    build_pivot_chronology_groups,
    chronology_membership,
    first_chronology_conflict,
)
from .schema import STANDARD_DEGREES
from .timeframes import (
    ProviderCapabilitySet,
    TimeframeUsePurpose,
    normalize_timeframe_name,
    timeframe_is_compatible_with_degree,
)


ADAPTIVE_RECOUNT_SCHEMA_VERSION = "adaptive-elliott-recount-1.0.0"
ADAPTIVE_RECOUNT_POLICY_VERSION = "adaptive-elliott-recount-policy-3.3.0"
ADAPTIVE_ROOT_CANDIDATE_SCHEMA_VERSION = "adaptive-root-candidate-3.3.0"
ADAPTIVE_TRACE_SCHEMA_VERSION = "adaptive-recount-trace-1.1.0"
ADAPTIVE_RECONSTRUCTION_REJECTION_SCHEMA_VERSION = (
    "adaptive-child-reconstruction-rejection-1.0.0"
)
ADAPTIVE_ROOT_OUTPUT_SCHEMA_VERSION = "adaptive-root-provider-output-1.1.0"
ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA_VERSION = (
    "adaptive-root-segmentation-provider-output-4.0.0"
)
ADAPTIVE_ROOT_CLASSIFICATION_OUTPUT_SCHEMA_VERSION = (
    "adaptive-root-classification-provider-output-1.1.0"
)
ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA_VERSION = (
    "adaptive-root-grouping-provider-output-1.0.0"
)
ADAPTIVE_ROOT_GROUP_CLASSIFICATION_OUTPUT_SCHEMA_VERSION = (
    "adaptive-root-group-classification-provider-output-1.1.0"
)
ADAPTIVE_ROOT_STAGE_FAILURE_SCHEMA_VERSION = (
    "adaptive-root-stage-validation-failure-1.1.0"
)
ADAPTIVE_ROOT_STAGE_REPAIR_CONTEXT_SCHEMA_VERSION = (
    "adaptive-root-stage-repair-context-1.1.0"
)
ADAPTIVE_ROOT_STAGE_DIAGNOSTIC_SCHEMA_VERSION = (
    "adaptive-root-stage-rejected-output-diagnostic-1.0.0"
)
ADAPTIVE_ROOT_STAGE_DIAGNOSTIC_REFERENCE_SCHEMA_VERSION = (
    "adaptive-root-stage-diagnostic-reference-1.0.0"
)
MAX_ROOT_STAGE_ATTEMPTS = 3
ROOT_STAGE_REPAIR_INSTRUCTION = (
    "return_materially_corrected_structured_output_with_a_new_semantic_signature"
)
RKLB_STRICT_BLIND_POLICY_ID = "rklb-strict-blind-recount-2026-05-27-v1"
_PIVOT_LINEAGE_ABSOLUTE_TOLERANCE = 0.0005
_ROOT_REPLAN_MINIMUM_TARGET_DISTANCE = 15
_ROOT_REPLAN_MINIMUM_IMPROVEMENT = 15


class AdaptiveRecountStatus(StrEnum):
    PLANNED = "planned"
    PENDING_DATA = "pending_data"
    READY_FOR_CANDIDATES = "ready_for_candidates"
    CANDIDATES_FROZEN = "candidates_frozen"
    RECURSIVE_PROOF_ACTIVE = "recursive_proof_active"
    READY_FOR_FINAL_COMPARISON = "ready_for_final_comparison"
    UNRESOLVED = "unresolved"
    NOT_COVERED = "not_covered"
    INCONSISTENT = "inconsistent"
    ROOT_CANDIDATE_GENERATION_FAILED = "root_candidate_generation_failed"
    FROZEN_BY_RESOLVE_DEGREES = "frozen_by_resolve_degrees"


class CandidateScreenStatus(StrEnum):
    SURVIVING_UNPROVEN = "surviving_unproven"
    NO_CANDIDATE = "no_candidate"
    REJECTED = "rejected"


class AdaptiveStopReason(StrEnum):
    NONE = "none"
    PENDING_NATIVE_DATA = "pending_native_data"
    MCP_UNAVAILABLE = "mcp_unavailable"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"
    RECURSION_DEPTH_EXHAUSTED = "recursion_depth_exhausted"
    GRAPH_BUDGET_EXHAUSTED = "graph_budget_exhausted"
    PIVOT_BUDGET_EXHAUSTED = "pivot_budget_exhausted"
    BAR_BUDGET_EXHAUSTED = "bar_budget_exhausted"
    PROOF_POLICY_CONFIGURATION_ERROR = "proof_policy_configuration_error"
    NO_VALID_CANDIDATE = "no_valid_candidate"
    ONE_VALID_CANDIDATE = "one_valid_candidate"
    TWO_VALID_CANDIDATES = "two_valid_candidates"
    LOWER_TIMEFRAME_NOT_COVERED = "lower_timeframe_not_covered"
    PENDING_STRUCTURAL_EVIDENCE = "pending_structural_evidence"
    RECOUNT_REQUIRED = "recount_required"
    CANDIDATE_SEARCH_EXHAUSTED = "candidate_search_exhausted"
    READY_FOR_FINAL_COMPARISON = "ready_for_final_comparison"
    ROOT_STAGE_CONTRACT_EXHAUSTED = "root_stage_contract_exhausted"


class RootGenerationStage(StrEnum):
    PRICE_SEGMENTATION = "stage_a_price_segmentation"
    STRUCTURAL_GROUPING = "stage_b1_structural_grouping"
    FAMILY_CLASSIFICATION = "stage_b2_family_classification"


class RootStageFailureCode(StrEnum):
    NON_INCREASING_PRICE_SEGMENT_BOUNDARIES = (
        "non_increasing_price_segment_boundaries"
    )
    DISCONNECTED_PRICE_SEGMENT_BOUNDARIES = (
        "disconnected_price_segment_boundaries"
    )
    REPEATED_BOUNDARY = "repeated_boundary"
    UNAVAILABLE_BOUNDARY = "unavailable_boundary"
    SEGMENTATION_DOES_NOT_BEGIN_AT_SCOPE = (
        "segmentation_does_not_begin_at_scope"
    )
    SEGMENTATION_DOES_NOT_END_AT_AS_OF = (
        "segmentation_does_not_end_at_as_of"
    )
    SEGMENT_DESCRIPTOR_COUNT_MISMATCH = "segment_descriptor_count_mismatch"
    SAME_BAR_INTRABAR_ORDER_UNKNOWN = "same_bar_intrabar_order_unknown"
    MUTUALLY_EXCLUSIVE_BOUNDARIES_SELECTED = (
        "mutually_exclusive_boundaries_selected"
    )
    INVALID_ATOMIC_SWING_EXTREMA = "invalid_atomic_swing_extrema"
    MALFORMED_STRUCTURED_OUTPUT = "malformed_structured_output"
    NON_CONTIGUOUS_STRUCTURAL_GROUP = "non_contiguous_structural_group"
    GROUP_BOUNDARY_MISMATCH = "group_boundary_mismatch"
    GROUPED_NODE_BOUNDARY_MISMATCH = "grouped_node_boundary_mismatch"
    INVALID_ROOT_DEGREE = "invalid_root_degree"
    DUPLICATE_ROOT_STAGE_CANDIDATE = "duplicate_root_stage_candidate"
    ROOT_STAGE_ATTEMPT_BUDGET_EXHAUSTED = (
        "root_stage_attempt_budget_exhausted"
    )


class RootStageParseError(ValueError):
    """Typed deterministic rejection raised after strict output-shape checks."""

    def __init__(
        self,
        error_code: RootStageFailureCode | str,
        message: str,
        *,
        affected_id: str | None = None,
        offending_boundary_ids: Sequence[str] = (),
        offending_boundary_ordinals: Sequence[int] = (),
        required_relation: str | None = None,
        chronology_group_id: str | None = None,
        chronology_group_ordinal: int | None = None,
        source_bundle_hash: str | None = None,
        source_bar_hash: str | None = None,
        shared_source_timestamp_utc: str | None = None,
        intrabar_order_status: str | None = None,
        repairable: bool = True,
        schema_conforming: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_code = RootStageFailureCode(error_code)
        self.affected_id = affected_id
        self.offending_boundary_ids = tuple(str(item) for item in offending_boundary_ids)
        self.offending_boundary_ordinals = tuple(int(item) for item in offending_boundary_ordinals)
        self.required_relation = required_relation
        self.chronology_group_id = chronology_group_id
        self.chronology_group_ordinal = chronology_group_ordinal
        self.source_bundle_hash = source_bundle_hash
        self.source_bar_hash = source_bar_hash
        self.shared_source_timestamp_utc = shared_source_timestamp_utc
        self.intrabar_order_status = intrabar_order_status
        self.repairable = bool(repairable)
        self.schema_conforming = bool(schema_conforming)


_ALLOWED_FAMILIES = frozenset((*MOTIVE_FAMILIES, *CORRECTIVE_FAMILIES))
_GROUP_CLASSIFICATION_FAMILIES: Mapping[SegmentClassificationKind, frozenset[str]] = {
    SegmentClassificationKind.IMPULSE: frozenset({"impulse"}),
    SegmentClassificationKind.LEADING_DIAGONAL: frozenset({"diagonal"}),
    SegmentClassificationKind.ENDING_DIAGONAL: frozenset({"diagonal"}),
    SegmentClassificationKind.ABC: frozenset({"zigzag", "flat"}),
    SegmentClassificationKind.ZIGZAG: frozenset({"zigzag"}),
    SegmentClassificationKind.WXY: frozenset({"combination"}),
    SegmentClassificationKind.WXYXZ: frozenset({"combination"}),
    SegmentClassificationKind.FLAT: frozenset({"flat"}),
    SegmentClassificationKind.TRIANGLE: frozenset({"triangle"}),
    SegmentClassificationKind.COMBINATION: frozenset({"combination"}),
}
_FORBIDDEN_PROVIDER_FIELDS = frozenset(
    {
        "verified",
        "verification_status",
        "confidence",
        "probability",
        "report_prose",
        "outcome",
        "lesson",
    }
)


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _hash(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
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
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _focused_bar_distance(value: int, *, minimum: int, maximum: int) -> int:
    if value < minimum:
        return minimum - value
    if value > maximum:
        return value - maximum
    return 0


def _actual_structural_bar_count(
    manifest: DataRequestManifest,
    bundle: NativeOHLCVBundle,
) -> int:
    start = _utc(manifest.structural_scoring_start_utc)
    end = _utc(manifest.structural_scoring_end_utc)
    return sum(start <= _utc(item.timestamp_utc) <= end for item in bundle.candles)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    method = getattr(value, "to_dict", None)
    if callable(method):
        return _json_value(method())
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    return value


class _Contract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class RootStageValidationFailure(_Contract):
    role: CandidateGraphRole
    stage: RootGenerationStage
    attempt_number: int
    packet_hash: str
    output_schema_hash: str
    returned_structured_output_hash: str | None
    semantic_signature: str | None
    error_code: RootStageFailureCode
    affected_id: str | None
    offending_boundary_ids: tuple[str, ...]
    offending_boundary_ordinals: tuple[int, ...]
    required_relation: str | None
    deterministic_error_message: str
    repairable: bool
    schema_conforming: bool
    created_at_utc: str
    chronology_group_id: str | None = None
    chronology_group_ordinal: int | None = None
    source_bundle_hash: str | None = None
    source_bar_hash: str | None = None
    shared_source_timestamp_utc: str | None = None
    intrabar_order_status: str | None = None
    schema_version: str = ADAPTIVE_ROOT_STAGE_FAILURE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", CandidateGraphRole(self.role))
        object.__setattr__(self, "stage", RootGenerationStage(self.stage))
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or not 1 <= self.attempt_number <= MAX_ROOT_STAGE_ATTEMPTS
        ):
            raise ValueError("Root-stage attempt_number must be between one and three.")
        for name in ("packet_hash", "output_schema_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        for name in ("returned_structured_output_hash", "semantic_signature"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _hash(value, field_name=name))
        object.__setattr__(self, "error_code", RootStageFailureCode(self.error_code))
        if self.affected_id is not None:
            object.__setattr__(
                self,
                "affected_id",
                _text(self.affected_id, field_name="affected_id"),
            )
        boundary_ids = tuple(
            _text(item, field_name="offending_boundary_id")
            for item in self.offending_boundary_ids
        )
        object.__setattr__(self, "offending_boundary_ids", boundary_ids)
        ordinals = tuple(self.offending_boundary_ordinals)
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in ordinals
        ):
            raise ValueError("offending_boundary_ordinals must be nonnegative integers.")
        object.__setattr__(self, "offending_boundary_ordinals", ordinals)
        if self.required_relation is not None:
            object.__setattr__(
                self,
                "required_relation",
                _text(self.required_relation, field_name="required_relation"),
            )
        if self.chronology_group_id is not None:
            object.__setattr__(
                self,
                "chronology_group_id",
                _text(self.chronology_group_id, field_name="chronology_group_id"),
            )
        if self.chronology_group_ordinal is not None and (
            not isinstance(self.chronology_group_ordinal, int)
            or isinstance(self.chronology_group_ordinal, bool)
            or self.chronology_group_ordinal < 0
        ):
            raise ValueError("chronology_group_ordinal must be nonnegative when supplied.")
        for name in ("source_bundle_hash", "source_bar_hash"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _hash(value, field_name=name))
        if self.shared_source_timestamp_utc is not None:
            object.__setattr__(
                self,
                "shared_source_timestamp_utc",
                _utc_text(
                    self.shared_source_timestamp_utc,
                    field_name="shared_source_timestamp_utc",
                ),
            )
        if self.intrabar_order_status is not None:
            object.__setattr__(
                self,
                "intrabar_order_status",
                IntrabarOrderStatus(self.intrabar_order_status).value,
            )
        object.__setattr__(
            self,
            "deterministic_error_message",
            _text(
                self.deterministic_error_message,
                field_name="deterministic_error_message",
            ),
        )
        if not isinstance(self.repairable, bool) or not isinstance(
            self.schema_conforming, bool
        ):
            raise TypeError("repairable and schema_conforming must be booleans.")
        if self.repairable and not self.schema_conforming:
            raise ValueError("Only schema-conforming root output may be repairable.")
        object.__setattr__(
            self,
            "created_at_utc",
            _utc_text(self.created_at_utc, field_name="created_at_utc"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and self.content_hash != root_stage_failure_content_hash(
            self
        ):
            raise ValueError("RootStageValidationFailure content hash is invalid.")

    @classmethod
    def create(cls, **values: Any) -> "RootStageValidationFailure":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=root_stage_failure_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "RootStageValidationFailure":
        raw = dict(value)
        raw["offending_boundary_ids"] = tuple(
            raw.get("offending_boundary_ids", ())
        )
        raw["offending_boundary_ordinals"] = tuple(
            raw.get("offending_boundary_ordinals", ())
        )
        result = cls(**raw)
        if verify_hash and result.content_hash != root_stage_failure_content_hash(result):
            raise ValueError("RootStageValidationFailure content hash is invalid.")
        return result

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        for name in (
            "chronology_group_id",
            "chronology_group_ordinal",
            "source_bundle_hash",
            "source_bar_hash",
            "shared_source_timestamp_utc",
            "intrabar_order_status",
        ):
            if getattr(self, name) is None:
                payload.pop(name, None)
        return payload


def root_stage_failure_content_hash(
    value: RootStageValidationFailure | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, RootStageValidationFailure) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class RootStageRepairFact(_Contract):
    error_code: RootStageFailureCode
    affected_id: str | None
    offending_boundary_ids: tuple[str, ...]
    offending_boundary_ordinals: tuple[int, ...]
    required_relation: str | None
    chronology_group_id: str | None = None
    chronology_group_ordinal: int | None = None
    source_bundle_hash: str | None = None
    source_bar_hash: str | None = None
    shared_source_timestamp_utc: str | None = None
    intrabar_order_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "error_code", RootStageFailureCode(self.error_code))
        if self.affected_id is not None:
            object.__setattr__(
                self,
                "affected_id",
                _text(self.affected_id, field_name="affected_id"),
            )
        object.__setattr__(
            self,
            "offending_boundary_ids",
            tuple(
                _text(item, field_name="offending_boundary_id")
                for item in self.offending_boundary_ids
            ),
        )
        ordinals = tuple(self.offending_boundary_ordinals)
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in ordinals
        ):
            raise ValueError("offending_boundary_ordinals must be nonnegative integers.")
        object.__setattr__(self, "offending_boundary_ordinals", ordinals)
        if self.required_relation is not None:
            object.__setattr__(
                self,
                "required_relation",
                _text(self.required_relation, field_name="required_relation"),
            )
        if self.chronology_group_id is not None:
            object.__setattr__(
                self,
                "chronology_group_id",
                _text(self.chronology_group_id, field_name="chronology_group_id"),
            )
        if self.chronology_group_ordinal is not None and (
            not isinstance(self.chronology_group_ordinal, int)
            or isinstance(self.chronology_group_ordinal, bool)
            or self.chronology_group_ordinal < 0
        ):
            raise ValueError("chronology_group_ordinal must be nonnegative when supplied.")
        for name in ("source_bundle_hash", "source_bar_hash"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _hash(value, field_name=name))
        if self.shared_source_timestamp_utc is not None:
            object.__setattr__(
                self,
                "shared_source_timestamp_utc",
                _utc_text(
                    self.shared_source_timestamp_utc,
                    field_name="shared_source_timestamp_utc",
                ),
            )
        if self.intrabar_order_status is not None:
            object.__setattr__(
                self,
                "intrabar_order_status",
                IntrabarOrderStatus(self.intrabar_order_status).value,
            )

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        for name in (
            "chronology_group_id",
            "chronology_group_ordinal",
            "source_bundle_hash",
            "source_bar_hash",
            "shared_source_timestamp_utc",
            "intrabar_order_status",
        ):
            if getattr(self, name) is None:
                payload.pop(name, None)
        return payload


@dataclass(frozen=True, slots=True)
class RootStageBoundaryOrdinal(_Contract):
    boundary_id: str
    boundary_ordinal: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "boundary_id",
            _text(self.boundary_id, field_name="boundary_id"),
        )
        if (
            not isinstance(self.boundary_ordinal, int)
            or isinstance(self.boundary_ordinal, bool)
            or self.boundary_ordinal < 0
        ):
            raise ValueError("boundary_ordinal must be a nonnegative integer.")


@dataclass(frozen=True, slots=True)
class RootStageRepairContext(_Contract):
    role: CandidateGraphRole
    stage: RootGenerationStage
    attempt_number: int
    excluded_output_hashes: tuple[str, ...]
    excluded_semantic_signatures: tuple[str, ...]
    deterministic_failures: tuple[RootStageRepairFact, ...]
    available_boundary_ordinals: tuple[RootStageBoundaryOrdinal, ...]
    scope_start_boundary_id: str | None
    as_of_boundary_id: str | None
    repair_instruction: str
    schema_version: str = ADAPTIVE_ROOT_STAGE_REPAIR_CONTEXT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", CandidateGraphRole(self.role))
        object.__setattr__(self, "stage", RootGenerationStage(self.stage))
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or not 2 <= self.attempt_number <= MAX_ROOT_STAGE_ATTEMPTS
        ):
            raise ValueError("Repair context attempt_number must be two or three.")
        for name in ("excluded_output_hashes", "excluded_semantic_signatures"):
            values = tuple(_hash(item, field_name=name[:-1]) for item in getattr(self, name))
            if len(values) != len(set(values)):
                raise ValueError(f"{name} cannot repeat.")
            object.__setattr__(self, name, values)
        failures = tuple(self.deterministic_failures)
        if not failures or any(not isinstance(item, RootStageRepairFact) for item in failures):
            raise ValueError("Repair context requires typed deterministic failures.")
        object.__setattr__(self, "deterministic_failures", failures)
        ordinals = tuple(self.available_boundary_ordinals)
        if len({item.boundary_id for item in ordinals}) != len(ordinals):
            raise ValueError("Repair-context boundary IDs cannot repeat.")
        if tuple(item.boundary_ordinal for item in ordinals) != tuple(range(len(ordinals))):
            raise ValueError("Repair-context boundary ordinals must be contiguous from zero.")
        object.__setattr__(self, "available_boundary_ordinals", ordinals)
        for name in ("scope_start_boundary_id", "as_of_boundary_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, field_name=name))
        if self.repair_instruction != ROOT_STAGE_REPAIR_INSTRUCTION:
            raise ValueError("Root-stage repair instruction must use the fixed policy value.")
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and self.content_hash != root_stage_repair_context_content_hash(self):
            raise ValueError("RootStageRepairContext content hash is invalid.")

    @classmethod
    def create(cls, **values: Any) -> "RootStageRepairContext":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(
            result,
            content_hash=root_stage_repair_context_content_hash(result),
        )


def root_stage_repair_context_content_hash(
    value: RootStageRepairContext | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, RootStageRepairContext) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class RejectedRootStageDiagnostic(_Contract):
    artifact_kind: str
    failure: RootStageValidationFailure
    structured_output: Mapping[str, Any]
    created_at_utc: str
    schema_version: str = ADAPTIVE_ROOT_STAGE_DIAGNOSTIC_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.artifact_kind != "non_evidence_root_stage_diagnostic":
            raise ValueError("Rejected root output must remain a non-evidence diagnostic.")
        if not isinstance(self.failure, RootStageValidationFailure):
            raise TypeError("failure must be a RootStageValidationFailure.")
        if not self.failure.schema_conforming:
            raise ValueError("Only schema-conforming rejected output may be persisted.")
        if not isinstance(self.structured_output, Mapping):
            raise TypeError("structured_output must be one strict JSON object.")
        normalized = _freeze_json(_json_value(self.structured_output))
        if canonical_sha256(_json_value(normalized)) != self.failure.returned_structured_output_hash:
            raise ValueError("Diagnostic output hash does not match its failure.")
        object.__setattr__(self, "structured_output", normalized)
        object.__setattr__(
            self,
            "created_at_utc",
            _utc_text(self.created_at_utc, field_name="created_at_utc"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and self.content_hash != rejected_root_stage_diagnostic_content_hash(self):
            raise ValueError("RejectedRootStageDiagnostic content hash is invalid.")

    @classmethod
    def create(cls, **values: Any) -> "RejectedRootStageDiagnostic":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(
            result,
            content_hash=rejected_root_stage_diagnostic_content_hash(result),
        )


def rejected_root_stage_diagnostic_content_hash(
    value: RejectedRootStageDiagnostic | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, RejectedRootStageDiagnostic) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class RootStageDiagnosticReference(_Contract):
    role: CandidateGraphRole
    stage: RootGenerationStage
    attempt_number: int
    failure_content_hash: str
    structured_output_hash: str
    artifact_content_hash: str
    artifact_path: str
    file_sha256: str
    schema_version: str = ADAPTIVE_ROOT_STAGE_DIAGNOSTIC_REFERENCE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", CandidateGraphRole(self.role))
        object.__setattr__(self, "stage", RootGenerationStage(self.stage))
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or not 1 <= self.attempt_number <= MAX_ROOT_STAGE_ATTEMPTS
        ):
            raise ValueError("Diagnostic attempt_number must be between one and three.")
        for name in (
            "failure_content_hash",
            "structured_output_hash",
            "artifact_content_hash",
            "file_sha256",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(
            self,
            "artifact_path",
            _text(self.artifact_path, field_name="artifact_path"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and self.content_hash != root_stage_diagnostic_reference_content_hash(self):
            raise ValueError("RootStageDiagnosticReference content hash is invalid.")

    @classmethod
    def create(cls, **values: Any) -> "RootStageDiagnosticReference":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(
            result,
            content_hash=root_stage_diagnostic_reference_content_hash(result),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "RootStageDiagnosticReference":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != root_stage_diagnostic_reference_content_hash(result):
            raise ValueError("RootStageDiagnosticReference content hash is invalid.")
        return result


def root_stage_diagnostic_reference_content_hash(
    value: RootStageDiagnosticReference | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, RootStageDiagnosticReference) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class AdaptiveRecountBudget(_Contract):
    maximum_recursion_depth: int = 3
    maximum_timeframe_requests: int = 8
    maximum_candidate_graphs: int = 20
    maximum_pivots: int = 2_000
    maximum_bars: int = 25_000
    terminal_degree: str = "Subminuette"

    def __post_init__(self) -> None:
        for name in (
            "maximum_recursion_depth",
            "maximum_timeframe_requests",
            "maximum_candidate_graphs",
            "maximum_pivots",
            "maximum_bars",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.maximum_recursion_depth > 8:
            raise ValueError("maximum_recursion_depth cannot exceed eight.")
        if self.terminal_degree not in STANDARD_DEGREES or self.terminal_degree == "Unassigned":
            raise ValueError("terminal_degree must be assigned.")


@dataclass(frozen=True, slots=True)
class AdaptiveRecountPlan(_Contract):
    plan_id: str
    symbol: str
    analysis_start_utc: str
    context_start_utc: str
    analysis_cutoff_utc: str
    blind: bool
    strict_symbol_history_exclusion: bool
    analysis_profile: str
    active_features: tuple[str, ...]
    market_data_provider: str
    provider_capabilities: ProviderCapabilitySet
    rules_pack: BlindCandidateRulesPack
    budgets: AdaptiveRecountBudget
    policy_id: str
    created_at_utc: str
    schema_version: str = ADAPTIVE_RECOUNT_SCHEMA_VERSION
    policy_version: str = ADAPTIVE_RECOUNT_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("plan_id", "symbol", "analysis_profile", "market_data_provider", "policy_id", "created_at_utc", "schema_version", "policy_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in ("analysis_start_utc", "context_start_utc", "analysis_cutoff_utc", "created_at_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        if _utc(self.context_start_utc) > _utc(self.analysis_start_utc):
            raise ValueError("context_start_utc cannot follow analysis_start_utc.")
        if _utc(self.analysis_start_utc) >= _utc(self.analysis_cutoff_utc):
            raise ValueError("analysis_start_utc must precede analysis_cutoff_utc.")
        if not self.blind or not self.strict_symbol_history_exclusion:
            raise ValueError("Adaptive recount v1 requires strict blind symbol-history exclusion.")
        expected_features = features_for_profile(self.analysis_profile)
        if tuple(self.active_features) != tuple(expected_features):
            raise ValueError("active_features must match the named analysis profile.")
        if not isinstance(self.provider_capabilities, ProviderCapabilitySet):
            raise TypeError("provider_capabilities must be a ProviderCapabilitySet.")
        if self.provider_capabilities.provider != self.market_data_provider:
            raise ValueError("Plan market-data provider does not match its capabilities.")
        if self.provider_capabilities.content_hash == "":
            raise ValueError("Provider capabilities must be content-hashed.")
        if not isinstance(self.rules_pack, BlindCandidateRulesPack):
            raise TypeError("rules_pack must be BlindCandidateRulesPack.")
        if self.rules_pack.content_hash != blind_candidate_rules_pack_content_hash(self.rules_pack):
            raise ValueError("Blind candidate rules pack hash is invalid.")
        if not isinstance(self.budgets, AdaptiveRecountBudget):
            raise TypeError("budgets must be AdaptiveRecountBudget.")
        if self.content_hash and self.content_hash != adaptive_recount_plan_content_hash(self):
            raise ValueError("AdaptiveRecountPlan content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveRecountPlan":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("plan_id"):
            values["plan_id"] = "adaptive_plan_" + canonical_sha256(
                {
                    "symbol": values.get("symbol"),
                    "start": values.get("analysis_start_utc"),
                    "cutoff": values.get("analysis_cutoff_utc"),
                    "created": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=adaptive_recount_plan_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "AdaptiveRecountPlan":
        raw = dict(value)
        raw["provider_capabilities"] = ProviderCapabilitySet.from_dict(raw["provider_capabilities"])
        raw["rules_pack"] = BlindCandidateRulesPack.from_dict(raw["rules_pack"])
        raw["budgets"] = AdaptiveRecountBudget(**dict(raw["budgets"]))
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_recount_plan_content_hash(result):
            raise ValueError("AdaptiveRecountPlan content_hash does not match its payload.")
        return result


def adaptive_recount_plan_content_hash(value: AdaptiveRecountPlan | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveRecountPlan) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def create_adaptive_recount_plan(
    *,
    symbol: str,
    analysis_start_utc: str,
    context_start_utc: str | None,
    analysis_cutoff_utc: str,
    market_data_provider: str,
    provider_capabilities: ProviderCapabilitySet,
    created_at_utc: str,
    analysis_profile: str = "elliott_full",
    budgets: AdaptiveRecountBudget | None = None,
    policy_id: str = "adaptive-blind-recount-v1",
) -> AdaptiveRecountPlan:
    return AdaptiveRecountPlan.create(
        plan_id="",
        symbol=symbol.upper().strip(),
        analysis_start_utc=analysis_start_utc,
        context_start_utc=context_start_utc or analysis_start_utc,
        analysis_cutoff_utc=analysis_cutoff_utc,
        blind=True,
        strict_symbol_history_exclusion=True,
        analysis_profile=analysis_profile,
        active_features=features_for_profile(analysis_profile),
        market_data_provider=market_data_provider,
        provider_capabilities=provider_capabilities,
        rules_pack=default_blind_candidate_rules_pack(),
        budgets=budgets or AdaptiveRecountBudget(),
        policy_id=policy_id,
        created_at_utc=created_at_utc,
    )


def rklb_strict_blind_plan(
    *,
    analysis_cutoff_utc: str,
    provider_capabilities: ProviderCapabilitySet,
    created_at_utc: str,
) -> AdaptiveRecountPlan:
    """Build the acceptance-case policy without supplying a count or pivot."""

    return create_adaptive_recount_plan(
        symbol="NASDAQ:RKLB",
        analysis_start_utc="2026-05-27T00:00:00+00:00",
        context_start_utc="2026-01-01T00:00:00+00:00",
        analysis_cutoff_utc=analysis_cutoff_utc,
        market_data_provider=provider_capabilities.provider,
        provider_capabilities=provider_capabilities,
        created_at_utc=created_at_utc,
        analysis_profile="elliott_full",
        policy_id=RKLB_STRICT_BLIND_POLICY_ID,
    )


def adaptive_child_policies(
    *,
    plan: AdaptiveRecountPlan,
    target_child_degree: str,
    target_timeframe: str,
) -> tuple[CandidateGenerationPolicy, SubdivisionVerifierPolicy]:
    """Build matching per-step policies for one planner-authorized descent."""

    target_timeframe = normalize_timeframe_name(target_timeframe)
    mapping = {target_child_degree: target_timeframe}
    policy_seed = canonical_sha256(
        {
            "plan": plan.content_hash,
            "child_degree": target_child_degree,
            "timeframe": target_timeframe,
            "depth": plan.budgets.maximum_recursion_depth,
            "terminal": plan.budgets.terminal_degree,
        }
    )[:20]
    return (
        CandidateGenerationPolicy.create(
            policy_id=f"adaptive-child-generation-{policy_seed}",
            maximum_generation_depth=plan.budgets.maximum_recursion_depth,
            terminal_degree=plan.budgets.terminal_degree,
            timeframe_by_child_degree=mapping,
            supporting_timeframes=(),
            policy_not_required_timeframes=(),
        ),
        SubdivisionVerifierPolicy.create(
            policy_id=f"adaptive-child-verifier-{policy_seed}",
            maximum_recursion_depth=plan.budgets.maximum_recursion_depth,
            terminal_degree=plan.budgets.terminal_degree,
            timeframe_by_child_degree=mapping,
            minimum_native_bars=6,
        ),
    )


@dataclass(frozen=True, slots=True)
class AdaptiveWaveCandidate(_Contract):
    wave_id: str
    parent_wave_id: str | None
    degree: str
    sequence_position: str
    timeframe: str
    direction: str
    declared_family: str
    completion_state: str
    start_pivot: TypedPivot
    end_pivot: TypedPivot
    invalidation: CandidateInvalidation | None
    child_wave_ids: tuple[str, ...]
    source_segment_id: str | None = None
    source_segment_ids: tuple[str, ...] = ()
    source_group_id: str | None = None
    source_grouping_hash: str | None = None
    diagonal_declaration: DiagonalDeclaration | None = None
    candidate_state: str = "candidate"

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _text(self.wave_id, field_name="wave_id"))
        if self.parent_wave_id is not None:
            object.__setattr__(self, "parent_wave_id", _text(self.parent_wave_id, field_name="parent_wave_id"))
        if self.degree not in STANDARD_DEGREES or self.degree == "Unassigned":
            raise ValueError("Adaptive candidate degree must be assigned.")
        object.__setattr__(self, "sequence_position", _text(self.sequence_position, field_name="sequence_position"))
        timeframe = normalize_timeframe_name(self.timeframe)
        object.__setattr__(self, "timeframe", timeframe)
        if not timeframe_is_compatible_with_degree(self.degree, timeframe):
            raise ValueError("Adaptive candidate timeframe is incompatible with its degree.")
        direction = _text(self.direction, field_name="direction").lower()
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down.")
        object.__setattr__(self, "direction", direction)
        family = _text(self.declared_family, field_name="declared_family").lower()
        if family not in _ALLOWED_FAMILIES:
            raise ValueError("declared_family is unsupported.")
        object.__setattr__(self, "declared_family", family)
        completion = _text(self.completion_state, field_name="completion_state").lower()
        if completion not in {"completed", "active", "projected"}:
            raise ValueError("completion_state is invalid.")
        object.__setattr__(self, "completion_state", completion)
        if not isinstance(self.start_pivot, TypedPivot) or not isinstance(self.end_pivot, TypedPivot):
            raise TypeError("Wave boundaries must be TypedPivot objects.")
        if _utc(self.end_pivot.timestamp_utc) <= _utc(self.start_pivot.timestamp_utc):
            raise ValueError("Wave end must follow wave start.")
        if self.invalidation is not None and not isinstance(self.invalidation, CandidateInvalidation):
            raise TypeError("invalidation must be CandidateInvalidation or null.")
        if self.source_segment_id is not None:
            object.__setattr__(self, "source_segment_id", _text(self.source_segment_id, field_name="source_segment_id"))
        source_segment_ids = tuple(
            _text(item, field_name="source_segment_id")
            for item in self.source_segment_ids
        )
        if len(source_segment_ids) != len(set(source_segment_ids)):
            raise ValueError("source_segment_ids cannot repeat.")
        object.__setattr__(self, "source_segment_ids", source_segment_ids)
        if self.source_segment_id is not None and source_segment_ids:
            raise ValueError("Use legacy source_segment_id or grouped source_segment_ids, not both.")
        if self.source_group_id is not None:
            object.__setattr__(
                self,
                "source_group_id",
                _text(self.source_group_id, field_name="source_group_id"),
            )
            if not source_segment_ids:
                raise ValueError("A grouped wave node requires source_segment_ids.")
            if self.source_grouping_hash is None:
                raise ValueError("A grouped wave node requires source_grouping_hash.")
        elif source_segment_ids or self.source_grouping_hash is not None:
            raise ValueError("Grouped segment lineage requires source_group_id.")
        if self.source_grouping_hash is not None:
            object.__setattr__(
                self,
                "source_grouping_hash",
                _hash(self.source_grouping_hash, field_name="source_grouping_hash"),
            )
        if self.diagonal_declaration is not None and not isinstance(
            self.diagonal_declaration, DiagonalDeclaration
        ):
            raise TypeError("diagonal_declaration must be DiagonalDeclaration or null.")
        if family != "diagonal" and self.diagonal_declaration is not None:
            raise ValueError("Only diagonal wave candidates may carry a diagonal declaration.")
        child_ids = tuple(_text(item, field_name="child_wave_id") for item in self.child_wave_ids)
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("child_wave_ids cannot repeat.")
        object.__setattr__(self, "child_wave_ids", child_ids)
        if self.candidate_state != "candidate":
            raise ValueError("Model-generated wave nodes must remain candidate-only.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdaptiveWaveCandidate":
        raw = dict(value)
        raw["start_pivot"] = TypedPivot.from_dict(raw["start_pivot"])
        raw["end_pivot"] = TypedPivot.from_dict(raw["end_pivot"])
        raw["invalidation"] = (
            CandidateInvalidation.from_dict(raw["invalidation"])
            if raw.get("invalidation") is not None
            else None
        )
        raw["diagonal_declaration"] = (
            DiagonalDeclaration.from_dict(raw["diagonal_declaration"])
            if raw.get("diagonal_declaration") is not None
            else None
        )
        raw["child_wave_ids"] = tuple(raw.get("child_wave_ids", ()))
        raw["source_segment_ids"] = tuple(raw.get("source_segment_ids", ()))
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.source_segment_id is None:
            payload.pop("source_segment_id", None)
        if not self.source_segment_ids:
            payload.pop("source_segment_ids", None)
        if self.source_group_id is None:
            payload.pop("source_group_id", None)
            payload.pop("source_grouping_hash", None)
        if self.diagonal_declaration is None:
            payload.pop("diagonal_declaration", None)
        return payload


@dataclass(frozen=True, slots=True)
class AdaptiveRootHypothesis(_Contract):
    hypothesis_id: str
    role: CandidateGraphRole
    plan_id: str
    plan_content_hash: str
    symbol: str
    analysis_start_utc: str
    analysis_cutoff_utc: str
    root_timeframe: str
    nodes: tuple[AdaptiveWaveCandidate, ...]
    active_wave_id: str | None
    confirmation_conditions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    source_bundle_hashes: tuple[str, ...]
    rules_pack_content_hash: str
    provider: str
    model: str | None
    generated_at_utc: str
    analysis_scope: AdaptiveAnalysisScope | None = None
    segmentation: PriceSegmentationHypothesis | None = None
    segment_classifications: tuple[SegmentClassification, ...] = ()
    root_grouping: RootStructuralGroupingHypothesis | None = None
    group_classifications: tuple[GroupClassification, ...] = ()
    candidate_evidence: tuple[CandidateScopedTechnicalEvidence, ...] = ()
    candidate_state: str = "candidate"
    schema_version: str = ADAPTIVE_ROOT_CANDIDATE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("hypothesis_id", "plan_id", "symbol", "provider", "generated_at_utc", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(self, "role", CandidateGraphRole(self.role))
        object.__setattr__(self, "plan_content_hash", _hash(self.plan_content_hash, field_name="plan_content_hash"))
        object.__setattr__(self, "rules_pack_content_hash", _hash(self.rules_pack_content_hash, field_name="rules_pack_content_hash"))
        for name in ("analysis_start_utc", "analysis_cutoff_utc", "generated_at_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "root_timeframe", normalize_timeframe_name(self.root_timeframe))
        nodes = tuple(self.nodes)
        if any(not isinstance(item, AdaptiveWaveCandidate) for item in nodes):
            raise ValueError("Root hypothesis nodes must be typed candidate nodes.")
        if not nodes and self.segmentation is None:
            raise ValueError("Legacy root hypotheses require typed candidate nodes.")
        if len({item.wave_id for item in nodes}) != len(nodes):
            raise ValueError("Root hypothesis wave IDs must be unique.")
        object.__setattr__(self, "nodes", nodes)
        if self.active_wave_id is not None and self.active_wave_id not in {item.wave_id for item in nodes}:
            raise ValueError("active_wave_id must identify a supplied node.")
        if self.candidate_state != "candidate":
            raise ValueError("Root hypotheses remain candidate-only.")
        object.__setattr__(self, "confirmation_conditions", tuple(str(item) for item in self.confirmation_conditions))
        object.__setattr__(self, "unresolved_questions", tuple(str(item) for item in self.unresolved_questions))
        hashes = tuple(_hash(item, field_name="source_bundle_hash") for item in self.source_bundle_hashes)
        if len(hashes) != len(set(hashes)):
            raise ValueError("source_bundle_hashes cannot repeat.")
        object.__setattr__(self, "source_bundle_hashes", hashes)
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        if self.analysis_scope is not None and not isinstance(self.analysis_scope, AdaptiveAnalysisScope):
            raise TypeError("analysis_scope must be AdaptiveAnalysisScope or null.")
        if self.segmentation is not None and not isinstance(self.segmentation, PriceSegmentationHypothesis):
            raise TypeError("segmentation must be PriceSegmentationHypothesis or null.")
        if (self.analysis_scope is None) != (self.segmentation is None):
            raise ValueError("analysis_scope and segmentation must be supplied together.")
        if self.root_grouping is not None and self.segmentation is None:
            raise ValueError("root_grouping requires a supplied segmentation.")
        classifications = tuple(self.segment_classifications)
        if any(not isinstance(item, SegmentClassification) for item in classifications):
            raise TypeError("segment_classifications must contain SegmentClassification values.")
        evidence = tuple(self.candidate_evidence)
        if any(not isinstance(item, CandidateScopedTechnicalEvidence) for item in evidence):
            raise TypeError("candidate_evidence must contain CandidateScopedTechnicalEvidence values.")
        object.__setattr__(self, "segment_classifications", classifications)
        if self.root_grouping is not None and not isinstance(
            self.root_grouping, RootStructuralGroupingHypothesis
        ):
            raise TypeError("root_grouping must be RootStructuralGroupingHypothesis or null.")
        group_classifications = tuple(self.group_classifications)
        if any(not isinstance(item, GroupClassification) for item in group_classifications):
            raise TypeError("group_classifications must contain GroupClassification values.")
        object.__setattr__(self, "group_classifications", group_classifications)
        object.__setattr__(self, "candidate_evidence", evidence)
        if self.segmentation is not None:
            if self.segmentation.analysis_scope_hash != self.analysis_scope.content_hash:
                raise ValueError("Segmentation does not belong to the supplied analysis scope.")
            segment_ids = {item.segment_id for item in self.segmentation.segments}
            evidence_ids = {item.evidence_id for item in evidence}
            if self.root_grouping is None:
                if {item.segment_id for item in classifications} != segment_ids:
                    raise ValueError("Every price segment requires exactly one classification.")
                if any(item.source_segment_id not in segment_ids for item in nodes):
                    raise ValueError("Every staged wave node must reference its source segment.")
                if group_classifications:
                    raise ValueError("Legacy segment classification cannot include group classifications.")
                referenced_evidence = (
                    evidence_id
                    for item in classifications
                    for evidence_id in item.evidence_ids
                )
            else:
                if classifications:
                    raise ValueError("Grouped root candidates cannot use legacy segment classifications.")
                if self.root_grouping.role != self.role.value:
                    raise ValueError("Root grouping role does not match its hypothesis.")
                self.root_grouping.validate_against(self.segmentation)
                group_by_id = {
                    item.group_id: item for item in self.root_grouping.groups
                }
                if {item.group_id for item in group_classifications} != set(group_by_id):
                    raise ValueError("Every structural group requires exactly one classification.")
                if len(group_classifications) != len(group_by_id):
                    raise ValueError("Structural group classifications cannot repeat a group.")
                wave_by_group: dict[str, set[str]] = {}
                segment_index = {
                    item.segment_id: index
                    for index, item in enumerate(self.segmentation.segments)
                }
                selected_group_ranges: list[tuple[int, int]] = []
                for node in nodes:
                    if node.source_group_id not in group_by_id:
                        raise ValueError("Every grouped wave node must reference its source group.")
                    group = group_by_id[node.source_group_id]
                    if node.source_segment_ids != group.source_segment_ids:
                        raise ValueError("Grouped wave segment lineage must exactly match its source group.")
                    if node.source_grouping_hash != self.root_grouping.content_hash:
                        raise ValueError("Grouped wave node references another grouping artifact.")
                    selected_group_ranges.append(
                        (
                            segment_index[group.source_segment_ids[0]],
                            segment_index[group.source_segment_ids[-1]],
                        )
                    )
                    wave_by_group.setdefault(node.source_group_id, set()).add(node.wave_id)
                selected_group_ranges.sort()
                if any(
                    right_start <= left_end
                    for (_, left_end), (right_start, _) in zip(
                        selected_group_ranges,
                        selected_group_ranges[1:],
                    )
                ):
                    raise ValueError(
                        "Selected grouped wave nodes cannot overlap at the same root layer."
                    )
                for classification in group_classifications:
                    actual = wave_by_group.get(classification.group_id, set())
                    declared = {classification.wave_id} if classification.wave_id is not None else set()
                    if declared != actual:
                        raise ValueError("Group classification wave reference does not match its node.")
                    if classification.wave_id is not None:
                        node = next(
                            item for item in nodes if item.wave_id == classification.wave_id
                        )
                        allowed_families = _GROUP_CLASSIFICATION_FAMILIES.get(
                            classification.classification
                        )
                        if (
                            allowed_families is not None
                            and node.declared_family not in allowed_families
                        ):
                            raise ValueError(
                                "Group classification conflicts with the candidate node family."
                            )
                referenced_evidence = (
                    evidence_id
                    for item in group_classifications
                    for evidence_id in item.evidence_ids
                )
            if any(evidence_id not in evidence_ids for evidence_id in referenced_evidence):
                raise ValueError("Classification references unknown technical evidence.")
        if self.content_hash and self.content_hash != adaptive_root_hypothesis_content_hash(self):
            raise ValueError("AdaptiveRootHypothesis content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveRootHypothesis":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=adaptive_root_hypothesis_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveRootHypothesis":
        raw = dict(value)
        raw["nodes"] = tuple(AdaptiveWaveCandidate.from_dict(item) for item in raw["nodes"])
        raw["confirmation_conditions"] = tuple(raw.get("confirmation_conditions", ()))
        raw["unresolved_questions"] = tuple(raw.get("unresolved_questions", ()))
        raw["source_bundle_hashes"] = tuple(raw.get("source_bundle_hashes", ()))
        raw["analysis_scope"] = (
            AdaptiveAnalysisScope.from_dict(raw["analysis_scope"])
            if raw.get("analysis_scope") is not None
            else None
        )
        raw["segmentation"] = (
            PriceSegmentationHypothesis.from_dict(raw["segmentation"])
            if raw.get("segmentation") is not None
            else None
        )
        raw["segment_classifications"] = tuple(
            SegmentClassification.from_dict(item)
            for item in raw.get("segment_classifications", ())
        )
        raw["root_grouping"] = (
            RootStructuralGroupingHypothesis.from_dict(raw["root_grouping"])
            if raw.get("root_grouping") is not None
            else None
        )
        raw["group_classifications"] = tuple(
            GroupClassification.from_dict(item)
            for item in raw.get("group_classifications", ())
        )
        raw["candidate_evidence"] = tuple(
            CandidateScopedTechnicalEvidence.from_dict(item)
            for item in raw.get("candidate_evidence", ())
        )
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_root_hypothesis_content_hash(result):
            raise ValueError("AdaptiveRootHypothesis content_hash does not match its payload.")
        return result

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.analysis_scope is None:
            payload.pop("analysis_scope", None)
            payload.pop("segmentation", None)
            payload.pop("segment_classifications", None)
            payload.pop("candidate_evidence", None)
        if self.root_grouping is None:
            payload.pop("root_grouping", None)
            payload.pop("group_classifications", None)
        return payload


def adaptive_root_hypothesis_content_hash(
    value: AdaptiveRootHypothesis | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveRootHypothesis) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class AdaptiveCandidateScreen(_Contract):
    hypothesis_id: str
    hypothesis_hash: str
    status: CandidateScreenStatus
    hard_rule_errors: tuple[str, ...]
    unresolved_requirements: tuple[str, ...]
    screened_at_utc: str
    hard_rule_calculations: tuple[Mapping[str, Any], ...] = ()
    boundary_validations: tuple[CandidateBoundaryValidity, ...] = ()
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_id", _text(self.hypothesis_id, field_name="hypothesis_id"))
        object.__setattr__(self, "hypothesis_hash", _hash(self.hypothesis_hash, field_name="hypothesis_hash"))
        object.__setattr__(self, "status", CandidateScreenStatus(self.status))
        object.__setattr__(self, "hard_rule_errors", tuple(str(item) for item in self.hard_rule_errors))
        object.__setattr__(self, "unresolved_requirements", tuple(str(item) for item in self.unresolved_requirements))
        object.__setattr__(self, "screened_at_utc", _utc_text(self.screened_at_utc, field_name="screened_at_utc"))
        calculations: list[Mapping[str, Any]] = []
        for item in self.hard_rule_calculations:
            if not isinstance(item, Mapping):
                raise TypeError("hard_rule_calculations must contain mappings.")
            calculations.append(dict(item))
        object.__setattr__(self, "hard_rule_calculations", tuple(calculations))
        boundaries = tuple(self.boundary_validations)
        if any(not isinstance(item, CandidateBoundaryValidity) for item in boundaries):
            raise TypeError("boundary_validations must contain CandidateBoundaryValidity values.")
        object.__setattr__(self, "boundary_validations", boundaries)
        if self.content_hash and self.content_hash != adaptive_candidate_screen_content_hash(self):
            raise ValueError("AdaptiveCandidateScreen content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveCandidateScreen":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=adaptive_candidate_screen_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveCandidateScreen":
        raw = dict(value)
        raw["hard_rule_errors"] = tuple(raw.get("hard_rule_errors", ()))
        raw["unresolved_requirements"] = tuple(raw.get("unresolved_requirements", ()))
        raw["hard_rule_calculations"] = tuple(raw.get("hard_rule_calculations", ()))
        raw["boundary_validations"] = tuple(
            CandidateBoundaryValidity.from_dict(item)
            for item in raw.get("boundary_validations", ())
        )
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_candidate_screen_content_hash(result):
            raise ValueError("AdaptiveCandidateScreen content_hash does not match its payload.")
        return result

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if not self.hard_rule_calculations:
            payload.pop("hard_rule_calculations", None)
        if not self.boundary_validations:
            payload.pop("boundary_validations", None)
        return payload


def adaptive_candidate_screen_content_hash(value: AdaptiveCandidateScreen | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveCandidateScreen) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


class AdaptiveRootCandidateProvider(Protocol):
    name: str
    model: str | None

    def generate_root_candidate(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


class AdaptiveStagedRootCandidateProvider(Protocol):
    """Strict two-stage provider; each role remains isolated from its peer."""

    name: str
    model: str | None

    def generate_root_segmentation(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def classify_root_segments(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


class AdaptiveGroupedRootCandidateProvider(Protocol):
    """Three-stage provider: price segments, structural groups, then families."""

    name: str
    model: str | None

    def generate_root_segmentation(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def generate_root_grouping(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def classify_root_groups(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


ADAPTIVE_ROOT_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "hypothesis_id",
        "root_timeframe",
        "nodes",
        "active_wave_id",
        "confirmation_conditions",
        "unresolved_questions",
    ],
    "properties": {
        "hypothesis_id": {"type": "string", "minLength": 1},
        "root_timeframe": {"type": "string", "minLength": 1},
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "wave_id",
                    "parent_wave_id",
                    "degree",
                    "sequence_position",
                    "timeframe",
                    "direction",
                    "declared_family",
                    "completion_state",
                    "start_pivot_id",
                    "end_pivot_id",
                    "invalidation",
                    "child_wave_ids",
                ],
                "properties": {
                    "wave_id": {"type": "string", "minLength": 1},
                    "parent_wave_id": {"type": ["string", "null"]},
                    "degree": {
                        "type": "string",
                        "enum": [item for item in STANDARD_DEGREES if item != "Unassigned"],
                    },
                    "sequence_position": {"type": "string", "minLength": 1},
                    "timeframe": {"type": "string", "minLength": 1},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "declared_family": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_FAMILIES),
                    },
                    "completion_state": {
                        "type": "string",
                        "enum": ["completed", "active", "projected"],
                    },
                    "start_pivot_id": {"type": "string", "minLength": 1},
                    "end_pivot_id": {"type": "string", "minLength": 1},
                    "invalidation": candidate_invalidation_openai_json_schema(),
                    "child_wave_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "active_wave_id": {"type": ["string", "null"]},
        "confirmation_conditions": {"type": "array", "items": {"type": "string"}},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
    },
}


def allowed_root_degrees_for_timeframes(
    timeframes: Sequence[str],
) -> tuple[str, ...]:
    """Return assigned standard degrees permitted by the evidence scale.

    This is a bounded compatibility set, not a degree decision. Duration,
    context, relative swing scale, and recursively proven anatomy remain the
    evidence used to distinguish among the permitted degree hypotheses.
    """

    if isinstance(timeframes, (str, bytes)) or not isinstance(timeframes, Sequence):
        raise TypeError("timeframes must be a sequence of canonical timeframe names.")
    canonical = tuple(
        sorted({normalize_timeframe_name(item) for item in timeframes})
    )
    if not canonical:
        raise ValueError("At least one root evidence timeframe is required.")
    allowed = tuple(
        degree
        for degree in STANDARD_DEGREES
        if degree != "Unassigned"
        and any(
            timeframe_is_compatible_with_degree(degree, timeframe)
            for timeframe in canonical
        )
    )
    if not allowed:
        raise ValueError("No Elliott degree is compatible with the root evidence scale.")
    return allowed


def adaptive_root_output_schema_for_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the root provider schema to validated evidence timeframes.

    The static contract describes the output shape. This derived schema also
    constrains semantic values that are known before the call, preventing a
    strict provider from returning a timeframe/degree pair that the immutable
    candidate contract must reject.
    """

    rows = packet.get("price_evidence_only") if isinstance(packet, Mapping) else None
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence) or not rows:
        raise ValueError("Root candidate packet requires price evidence.")
    timeframes: set[str] = set()
    for row in rows:
        identity = row.get("bundle_identity") if isinstance(row, Mapping) else None
        if not isinstance(identity, Mapping):
            raise ValueError("Root candidate packet bundle identity is invalid.")
        timeframes.add(normalize_timeframe_name(identity.get("timeframe")))
    # Lexical order is stable, and the current root workflow supplies one
    # validated native timeframe. Semantic pair validation remains downstream
    # for any future multi-bundle root packet.
    instructions = packet.get("instructions")
    ordered_timeframes = sorted(timeframes)
    configured = (
        instructions.get("allowed_root_degrees")
        if isinstance(instructions, Mapping)
        else None
    )
    if configured is None:
        # Read-only compatibility for packets produced before root discovery
        # became degree-neutral.
        legacy_expected = (
            instructions.get("expected_root_degree")
            if isinstance(instructions, Mapping)
            else None
        )
        allowed_degrees = (
            [legacy_expected]
            if legacy_expected is not None
            else list(allowed_root_degrees_for_timeframes(ordered_timeframes))
        )
    else:
        if isinstance(configured, (str, bytes)) or not isinstance(configured, Sequence):
            raise ValueError("allowed_root_degrees must be an array.")
        allowed_degrees = list(configured)
    derived = set(allowed_root_degrees_for_timeframes(ordered_timeframes))
    if (
        not allowed_degrees
        or len(set(allowed_degrees)) != len(allowed_degrees)
        or any(
            degree not in STANDARD_DEGREES
            or degree == "Unassigned"
            or degree not in derived
            for degree in allowed_degrees
        )
    ):
        raise ValueError("Root candidate packet allowed degrees are invalid.")
    if not ordered_timeframes:
        raise ValueError("The root candidate packet has no evidence timeframe.")

    schema = deepcopy(ADAPTIVE_ROOT_OUTPUT_SCHEMA)
    properties = schema["properties"]
    properties["root_timeframe"] = {
        "type": "string",
        "enum": ordered_timeframes,
    }
    node_properties = properties["nodes"]["items"]["properties"]
    node_properties["timeframe"] = {
        "type": "string",
        "enum": ordered_timeframes,
    }
    node_properties["degree"] = {
        "type": "string",
        "enum": allowed_degrees,
    }
    return schema


def _bundle_dataset_cutoff(bundle: NativeOHLCVBundle) -> DatasetCutoff:
    return DatasetCutoff(
        dataset_id=bundle.bundle_id,
        source_reference=bundle.source_request_id,
        timeframe=bundle.timeframe,
        cutoff_utc=bundle.analysis_cutoff_utc,
        dataset_hash=bundle.content_hash,
        hash_scope=DatasetHashScope.SOURCE_DOCUMENT,
        provider=bundle.provider,
        feed_identity=bundle.feed_identity,
        requested_symbol=bundle.canonical_symbol,
        resolved_symbol=bundle.provider_symbol,
        exchange=bundle.exchange,
        session=bundle.session,
        timezone=bundle.timezone,
        adjustment={
            "price": bundle.price_adjustment,
            "dividend": bundle.dividend_adjustment,
            "volume": bundle.volume_adjustment,
        },
        price_basis=bundle.price_basis,
        completed_candles_only=True,
        source_document_hash=bundle.content_hash,
        captured_at_utc=bundle.acquisition_timestamp_utc,
        bar_count=len(bundle.candles),
    )


def native_window_from_bundle(
    bundle: NativeOHLCVBundle,
    *,
    coverage_role: WindowCoverageRole = WindowCoverageRole.REQUIRED_GENERATION,
) -> NativeOHLCVWindow:
    return NativeOHLCVWindow.create(
        window_id=bundle.bundle_id,
        dataset_cutoff=_bundle_dataset_cutoff(bundle),
        timeframe=bundle.timeframe,
        candles=bundle.candles,
        coverage_role=coverage_role,
        is_native=True,
    )


def build_blind_root_packet(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
    *,
    role: CandidateGraphRole,
    expected_degree: str | None = None,
) -> Mapping[str, Any]:
    """Build a price-only packet with no prior symbol analysis or peer output."""

    bundle_rows: list[dict[str, Any]] = []
    for bundle in bundles:
        window = native_window_from_bundle(bundle)
        catalog = build_native_pivot_catalog(window)
        if len(catalog) > plan.budgets.maximum_pivots:
            raise ValueError("Pivot budget exhausted before candidate generation.")
        bundle_rows.append(
            {
                "bundle_identity": {
                    "bundle_id": bundle.bundle_id,
                    "content_hash": bundle.content_hash,
                    "symbol": bundle.canonical_symbol,
                    "provider": bundle.provider,
                    "feed_identity": bundle.feed_identity,
                    "feed_family_hash": bundle.feed_family.feed_family_hash,
                    "stream_hash": bundle.stream_identity.stream_hash,
                    "timeframe": bundle.timeframe,
                    "session": bundle.session,
                    "adjustment": bundle.price_adjustment,
                    "price_basis": bundle.price_basis,
                    "cutoff": bundle.analysis_cutoff_utc,
                },
                "native_candles": [
                    {
                        "candle_id": candle.candle_id,
                        "timestamp_utc": candle.timestamp_utc,
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "volume_available": candle.volume is not None,
                        "source_row_hash": candle.source_row_hash,
                    }
                    for candle in bundle.candles
                ],
                "pivot_catalog": [item.to_dict() for item in catalog],
            }
        )
    if expected_degree is not None and (
        expected_degree not in STANDARD_DEGREES or expected_degree == "Unassigned"
    ):
        raise ValueError("expected_degree must be an assigned standard degree.")
    # ``expected_degree`` remains accepted only as a source-compatibility shim
    # for older callers. It never narrows a new blind root-discovery packet.
    allowed_root_degrees = allowed_root_degrees_for_timeframes(
        tuple(row["bundle_identity"]["timeframe"] for row in bundle_rows)
    )
    return {
        "schema_version": ADAPTIVE_ROOT_OUTPUT_SCHEMA_VERSION,
        "candidate_role": role.value,
        "blind_isolation": {
            "strict": True,
            "peer_candidate_available": False,
            "prior_symbol_counts_available": False,
            "accepted_cases_available": False,
            "forecasts_outcomes_lessons_available": False,
        },
        "plan": {
            "plan_id": plan.plan_id,
            "plan_content_hash": plan.content_hash,
            "symbol": plan.symbol,
            "analysis_start_utc": plan.analysis_start_utc,
            "context_start_utc": plan.context_start_utc,
            "analysis_cutoff_utc": plan.analysis_cutoff_utc,
            "analysis_window_is_not_forced_origin": True,
        },
        "rules_pack": plan.rules_pack.to_dict(),
        "price_evidence_only": bundle_rows,
        "instructions": {
            "candidate_only": True,
            "catalog_pivots_only": True,
            "indicators_not_supplied": True,
            "allowed_root_degrees": list(allowed_root_degrees),
            "root_degree_is_provisional": True,
            "analysis_scope_has_no_elliott_degree": True,
            "verification_owner": "deterministic_subdivision_verifier",
        },
    }


ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA_V3: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segmentation_id", "boundary_path", "segments"],
    "properties": {
        "segmentation_id": {"type": "string", "minLength": 1},
        "boundary_path": {
            "type": "array",
            "minItems": 2,
            "items": {"type": "string", "minLength": 1},
        },
        "segments": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "segment_id",
                    "completion_state",
                    "segment_kind",
                ],
                "properties": {
                    "segment_id": {"type": "string", "minLength": 1},
                    "completion_state": {
                        "type": "string",
                        "enum": [item.value for item in SegmentCompletionState],
                    },
                    "segment_kind": {
                        "type": "string",
                        "enum": [item.value for item in PriceSegmentKind],
                    },
                },
            },
        },
    },
}


# Production Stage A v4 has one authored decision per interval. The scope start
# is implicit, segment IDs and completion states are deterministic, and the
# final step must end at the as-of observation.
ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segmentation_id", "steps"],
    "properties": {
        "segmentation_id": {"type": "string", "minLength": 1},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["end_boundary_id", "segment_kind"],
                "properties": {
                    "end_boundary_id": {"type": "string", "minLength": 1},
                    "segment_kind": {
                        "type": "string",
                        "enum": [item.value for item in PriceSegmentKind],
                    },
                },
            },
        },
    },
}


ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["grouping_id", "groups", "unresolved_segment_ids"],
    "properties": {
        "grouping_id": {"type": "string", "minLength": 1},
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "group_id",
                    "source_segment_ids",
                    "start_boundary_id",
                    "end_boundary_id",
                    "completion_state",
                ],
                "properties": {
                    "group_id": {"type": "string", "minLength": 1},
                    "source_segment_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "start_boundary_id": {"type": "string", "minLength": 1},
                    "end_boundary_id": {"type": "string", "minLength": 1},
                    "completion_state": {
                        "type": "string",
                        "enum": [item.value for item in SegmentCompletionState],
                    },
                },
            },
        },
        "unresolved_segment_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}


def _root_classification_schema() -> dict[str, Any]:
    node_schema = deepcopy(ADAPTIVE_ROOT_OUTPUT_SCHEMA["properties"]["nodes"]["items"])
    node_schema["required"] = [
        *node_schema["required"],
        "source_segment_id",
        "diagonal_declaration",
    ]
    node_schema["properties"]["source_segment_id"] = {
        "type": "string",
        "minLength": 1,
    }
    node_schema["properties"]["invalidation"] = (
        optional_candidate_invalidation_openai_json_schema()
    )
    node_schema["properties"]["diagonal_declaration"] = {
        "anyOf": [
            diagonal_declaration_openai_json_schema(nullable=False),
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "hypothesis_id",
            "root_timeframe",
            "nodes",
            "segment_classifications",
            "active_wave_id",
            "confirmation_conditions",
            "unresolved_questions",
        ],
        "properties": {
            "hypothesis_id": {"type": "string", "minLength": 1},
            "root_timeframe": {"type": "string", "minLength": 1},
            "nodes": {"type": "array", "items": node_schema},
            "segment_classifications": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "segment_id",
                        "classification",
                        "wave_ids",
                        "evidence_ids",
                    ],
                    "properties": {
                        "segment_id": {"type": "string", "minLength": 1},
                        "classification": {
                            "type": "string",
                            "enum": [item.value for item in SegmentClassificationKind],
                        },
                        "wave_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "active_wave_id": {"type": ["string", "null"]},
            "confirmation_conditions": {"type": "array", "items": {"type": "string"}},
            "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        },
    }


ADAPTIVE_ROOT_CLASSIFICATION_OUTPUT_SCHEMA: Mapping[str, Any] = (
    _root_classification_schema()
)


def _root_group_classification_schema() -> dict[str, Any]:
    node_schema = deepcopy(ADAPTIVE_ROOT_OUTPUT_SCHEMA["properties"]["nodes"]["items"])
    node_schema["required"] = [
        *node_schema["required"],
        "source_group_id",
        "source_segment_ids",
        "diagonal_declaration",
    ]
    node_schema["properties"]["source_group_id"] = {
        "type": "string",
        "minLength": 1,
    }
    node_schema["properties"]["source_segment_ids"] = {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "minLength": 1},
    }
    node_schema["properties"]["invalidation"] = (
        optional_candidate_invalidation_openai_json_schema()
    )
    node_schema["properties"]["diagonal_declaration"] = {
        "anyOf": [
            diagonal_declaration_openai_json_schema(nullable=False),
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "hypothesis_id",
            "root_timeframe",
            "nodes",
            "group_classifications",
            "active_wave_id",
            "confirmation_conditions",
            "unresolved_questions",
        ],
        "properties": {
            "hypothesis_id": {"type": "string", "minLength": 1},
            "root_timeframe": {"type": "string", "minLength": 1},
            "nodes": {"type": "array", "items": node_schema},
            "group_classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "group_id",
                        "classification",
                        "wave_id",
                        "evidence_ids",
                    ],
                    "properties": {
                        "group_id": {"type": "string", "minLength": 1},
                        "classification": {
                            "type": "string",
                            "enum": [item.value for item in SegmentClassificationKind],
                        },
                        "wave_id": {"type": ["string", "null"]},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "active_wave_id": {"type": ["string", "null"]},
            "confirmation_conditions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "unresolved_questions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


ADAPTIVE_ROOT_GROUP_CLASSIFICATION_OUTPUT_SCHEMA: Mapping[str, Any] = (
    _root_group_classification_schema()
)


def _scope_bundle(bundles: Sequence[NativeOHLCVBundle]) -> NativeOHLCVBundle:
    if not bundles:
        raise ValueError("At least one validated bundle is required.")
    ordered = sorted(
        bundles,
        key=lambda item: len(item.candles),
        reverse=True,
    )
    return ordered[0]


def build_adaptive_analysis_scope(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
) -> AdaptiveAnalysisScope:
    """Build a non-Elliott container from immutable source observations."""

    bundle = _scope_bundle(bundles)
    rows = tuple(
        item
        for item in bundle.candles
        if _utc(item.timestamp_utc) >= _utc(plan.analysis_start_utc)
        and _utc(item.timestamp_utc) <= _utc(plan.analysis_cutoff_utc)
    )
    if len(rows) < 2:
        raise ValueError("Analysis scope requires at least two completed source candles.")
    first, last = rows[0], rows[-1]
    return AdaptiveAnalysisScope.create(
        scope_id="",
        symbol=plan.symbol,
        timeframe=bundle.timeframe,
        window_start=AnalysisScopeBoundary(
            boundary_id=f"scope_start:{plan.analysis_start_utc}:{first.candle_id}",
            kind=AnalysisBoundaryKind.WINDOW_START,
            timestamp_utc=plan.analysis_start_utc,
            price=first.open,
            source_pivot_id=None,
            source_bar_hash=first.source_row_hash,
            is_elliott_pivot=False,
        ),
        as_of_observation=AnalysisScopeBoundary(
            boundary_id=f"scope_asof:{last.candle_id}",
            kind=AnalysisBoundaryKind.AS_OF_OBSERVATION,
            timestamp_utc=last.timestamp_utc,
            price=last.close,
            source_pivot_id=None,
            source_bar_hash=last.source_row_hash,
            is_elliott_pivot=False,
        ),
        analysis_cutoff_utc=plan.analysis_cutoff_utc,
        source_bundle_hashes=tuple(bundle.content_hash for bundle in bundles),
    )


def _scope_boundaries(
    scope: AdaptiveAnalysisScope,
    bundles: Sequence[NativeOHLCVBundle],
) -> tuple[AnalysisScopeBoundary, ...]:
    boundaries: list[AnalysisScopeBoundary] = [scope.window_start]
    seen = {scope.window_start.boundary_id, scope.as_of_observation.boundary_id}
    for bundle in bundles:
        for pivot in build_native_pivot_catalog(native_window_from_bundle(bundle)):
            timestamp = _utc(pivot.timestamp_utc)
            if not (
                _utc(scope.window_start.timestamp_utc)
                < timestamp
                < _utc(scope.as_of_observation.timestamp_utc)
            ):
                continue
            boundary_id = f"catalog:{pivot.pivot_id}"
            if boundary_id in seen:
                continue
            seen.add(boundary_id)
            boundaries.append(
                AnalysisScopeBoundary(
                    boundary_id=boundary_id,
                    kind=AnalysisBoundaryKind.CATALOG_PIVOT,
                    timestamp_utc=pivot.timestamp_utc,
                    price=pivot.price,
                    source_pivot_id=pivot.pivot_id,
                    source_bar_hash=pivot.source_bar_hash,
                    is_elliott_pivot=True,
                    extremum_type=PriceExtremumType(pivot.pivot_type.value),
                )
            )
    boundaries.append(scope.as_of_observation)
    return tuple(sorted(boundaries, key=lambda item: _utc(item.timestamp_utc)))


def _boundary_chronology_groups(
    scope: AdaptiveAnalysisScope,
    bundles: Sequence[NativeOHLCVBundle],
    boundaries: Sequence[AnalysisScopeBoundary] | None = None,
) -> tuple[PivotChronologyGroup, ...]:
    """Build source-candle chronology without inventing intrabar order."""

    ordered_boundaries = tuple(boundaries or _scope_boundaries(scope, bundles))
    bundle_by_row: dict[str, list[tuple[NativeOHLCVBundle, Any]]] = {}
    for bundle in bundles:
        for candle in bundle.candles:
            bundle_by_row.setdefault(candle.source_row_hash, []).append(
                (bundle, candle)
            )
    members: list[PivotChronologyMember] = []
    for boundary in ordered_boundaries:
        candidates = bundle_by_row.get(boundary.source_bar_hash, [])
        selected_pair = next(
            (
                (bundle, candle)
                for bundle, candle in candidates
                if boundary.source_pivot_id is not None
                and boundary.source_pivot_id.startswith(f"{bundle.bundle_id}:")
            ),
            candidates[0] if candidates else None,
        )
        selected = selected_pair[0] if selected_pair is not None else None
        source_timestamp = (
            selected_pair[1].timestamp_utc
            if selected_pair is not None
            else boundary.timestamp_utc
        )
        source_bundle_hash = (
            selected.content_hash
            if selected is not None
            else scope.source_bundle_hashes[0]
        )
        timeframe = selected.timeframe if selected is not None else scope.timeframe
        members.append(
            PivotChronologyMember(
                boundary_id=boundary.boundary_id,
                extremum=(
                    ChronologyExtremum(boundary.extremum_type.value)
                    if boundary.extremum_type is not None
                    else ChronologyExtremum.OBSERVATION
                ),
                source_bundle_hash=source_bundle_hash,
                source_bar_hash=boundary.source_bar_hash,
                source_candle_timestamp_utc=source_timestamp,
                timeframe=timeframe,
            )
        )
    return build_pivot_chronology_groups(tuple(members))


def build_root_segmentation_packet(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
    *,
    role: CandidateGraphRole,
    scope: AdaptiveAnalysisScope,
) -> Mapping[str, Any]:
    bundle_rows = []
    context_rows = []
    for bundle in bundles:
        identity = {
            "bundle_id": bundle.bundle_id,
            "content_hash": bundle.content_hash,
            "timeframe": bundle.timeframe,
            "provider": bundle.provider,
            "feed_identity": bundle.feed_identity,
            "feed_family_hash": bundle.feed_family.feed_family_hash,
            "stream_hash": bundle.stream_identity.stream_hash,
            "cutoff": bundle.analysis_cutoff_utc,
        }
        bundle_rows.append(
            {
                "bundle_identity": identity,
                "native_price_candles": [
                    {
                        "candle_id": candle.candle_id,
                        "timestamp_utc": candle.timestamp_utc,
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "source_row_hash": candle.source_row_hash,
                    }
                    for candle in bundle.candles
                    if _utc(scope.window_start.timestamp_utc)
                    <= _utc(candle.timestamp_utc)
                    <= _utc(scope.as_of_observation.timestamp_utc)
                ],
            }
        )
        context_rows.append(
            {
                "bundle_identity": identity,
                "native_price_candles": [
                    {
                        "candle_id": candle.candle_id,
                        "timestamp_utc": candle.timestamp_utc,
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "source_row_hash": candle.source_row_hash,
                    }
                    for candle in bundle.candles
                    if _utc(plan.context_start_utc)
                    <= _utc(candle.timestamp_utc)
                    < _utc(plan.analysis_start_utc)
                ],
            }
        )
    boundaries = _scope_boundaries(scope, bundles)
    chronology_groups = _boundary_chronology_groups(
        scope,
        bundles,
        boundaries,
    )
    membership = chronology_membership(chronology_groups)
    return {
        "schema_version": ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA_VERSION,
        "candidate_role": CandidateGraphRole(role).value,
        "blind_isolation": {
            "strict": True,
            "peer_candidate_available": False,
            "prior_symbol_counts_available": False,
            "accepted_cases_available": False,
            "forecasts_outcomes_lessons_available": False,
        },
        "plan_content_hash": plan.content_hash,
        "rules_pack_content_hash": plan.rules_pack.content_hash,
        "analysis_scope": scope.to_dict(),
        "intervals": {
            "context_window": {
                "start_utc": plan.context_start_utc,
                "end_exclusive_utc": plan.analysis_start_utc,
                "mandatory_segmentation": False,
            },
            "requested_analysis_window": {
                "start_utc": plan.analysis_start_utc,
                "cutoff_utc": plan.analysis_cutoff_utc,
                "mandatory_segmentation": True,
            },
            "as_of_observation": scope.as_of_observation.to_dict(),
        },
        "available_boundaries": [
            {
                **item.to_dict(),
                "boundary_ordinal": ordinal,
                "chronology_group_id": membership[item.boundary_id].chronology_group_id,
                "chronology_ordinal": membership[item.boundary_id].chronology_ordinal,
                "intrabar_order_status": membership[
                    item.boundary_id
                ].intrabar_order_status.value,
            }
            for ordinal, item in enumerate(boundaries)
        ],
        "mutually_exclusive_boundary_groups": [
            item.to_dict()
            for item in chronology_groups
            if len(item.member_boundary_ids) > item.maximum_selectable_members
        ],
        "context_price_evidence_only": context_rows,
        "price_evidence_only": bundle_rows,
        "instructions": {
            "stage": "price_segmentation",
            "elliott_labels_forbidden": True,
            "cover_complete_scope": True,
            "connected_boundaries_required": True,
            "ordered_step_path_required": True,
            "scope_start_is_implicit": True,
            "each_step_selects_the_next_end_boundary": True,
            "chronology_group_ordinals_must_strictly_increase": True,
            "boundary_ordinal_is_display_compatibility_only": True,
            "select_at_most_one_member_from_each_chronology_group": True,
            "intrabar_order_policy": (
                "Select at most one member from each chronology group unless a "
                "finer native dataset has deterministically resolved the order."
            ),
            "window_start_and_as_of_are_not_elliott_pivots": True,
            "context_precedes_analysis_window": True,
            "context_candles_are_not_required_segments_or_elliott_nodes": True,
            "first_requested_segment_may_be_previous_parent_wave_tail": True,
            "segment_kinds_required": [item.value for item in PriceSegmentKind],
            "closed_atomic_swings_require_opposite_typed_extrema": True,
            "same_type_extrema_require_continuation_or_unresolved_kind": True,
            "segment_ids_are_deterministic": True,
            "completion_state_is_deterministic": True,
            "all_nonfinal_segments_are_closed": True,
            "final_as_of_segment_is_active": True,
        },
    }


def _validate_root_boundary_path(
    boundary_path: Sequence[str],
    *,
    scope: AdaptiveAnalysisScope,
    boundaries: Mapping[str, AnalysisScopeBoundary],
    boundary_ordinals: Mapping[str, int],
    chronology_groups: Sequence[PivotChronologyGroup],
    affected_ids: Sequence[str],
    require_scope_coverage: bool = True,
) -> None:
    repeated = tuple(
        item
        for index, item in enumerate(boundary_path)
        if item in boundary_path[:index]
    )
    if repeated:
        raise RootStageParseError(
            RootStageFailureCode.REPEATED_BOUNDARY,
            "A segmentation path cannot repeat a boundary.",
            offending_boundary_ids=repeated,
        )
    missing = tuple(item for item in boundary_path if item not in boundaries)
    if missing:
        raise RootStageParseError(
            RootStageFailureCode.UNAVAILABLE_BOUNDARY,
            "A segmentation path references an unavailable boundary.",
            offending_boundary_ids=missing,
        )
    if require_scope_coverage and boundary_path[0] != scope.window_start.boundary_id:
        raise RootStageParseError(
            RootStageFailureCode.SEGMENTATION_DOES_NOT_BEGIN_AT_SCOPE,
            "Segmentation must begin at the analysis-scope boundary.",
            offending_boundary_ids=(
                boundary_path[0],
                scope.window_start.boundary_id,
            ),
            offending_boundary_ordinals=(boundary_ordinals[boundary_path[0]], 0),
            required_relation="boundary_path[0] == scope_start_boundary_id",
        )
    if require_scope_coverage and boundary_path[-1] != scope.as_of_observation.boundary_id:
        raise RootStageParseError(
            RootStageFailureCode.SEGMENTATION_DOES_NOT_END_AT_AS_OF,
            "Segmentation must end at the as-of observation.",
            offending_boundary_ids=(
                boundary_path[-1],
                scope.as_of_observation.boundary_id,
            ),
            offending_boundary_ordinals=(
                boundary_ordinals[boundary_path[-1]],
                boundary_ordinals[scope.as_of_observation.boundary_id],
            ),
            required_relation="boundary_path[-1] == as_of_boundary_id",
        )
    conflict = first_chronology_conflict(boundary_path, chronology_groups)
    if conflict is not None:
        code = (
            RootStageFailureCode.SAME_BAR_INTRABAR_ORDER_UNKNOWN
            if conflict.intrabar_order_status is IntrabarOrderStatus.UNKNOWN
            else RootStageFailureCode.MUTUALLY_EXCLUSIVE_BOUNDARIES_SELECTED
        )
        raise RootStageParseError(
            code,
            "The selected boundaries are mutually exclusive extrema from one native candle; their intrabar order is unknown.",
            affected_id=(affected_ids[max(0, len(conflict.selected_boundary_ids) - 2)] if affected_ids else None),
            offending_boundary_ids=conflict.selected_boundary_ids,
            offending_boundary_ordinals=tuple(
                boundary_ordinals[item] for item in conflict.selected_boundary_ids
            ),
            required_relation="select_at_most_one_boundary_from_chronology_group",
            chronology_group_id=conflict.chronology_group_id,
            chronology_group_ordinal=conflict.chronology_ordinal,
            source_bundle_hash=conflict.source_bundle_hash,
            source_bar_hash=conflict.source_bar_hash,
            shared_source_timestamp_utc=conflict.source_candle_timestamp_utc,
            intrabar_order_status=conflict.intrabar_order_status.value,
        )
    membership = chronology_membership(chronology_groups)
    for index, (left_id, right_id) in enumerate(
        zip(boundary_path, boundary_path[1:])
    ):
        left_group = membership[left_id]
        right_group = membership[right_id]
        if (
            right_group.chronology_ordinal <= left_group.chronology_ordinal
            or _utc(boundaries[right_id].timestamp_utc)
            <= _utc(boundaries[left_id].timestamp_utc)
        ):
            raise RootStageParseError(
                RootStageFailureCode.NON_INCREASING_PRICE_SEGMENT_BOUNDARIES,
                "Price segment end must follow its start by source chronology.",
                affected_id=(affected_ids[index] if index < len(affected_ids) else None),
                offending_boundary_ids=(left_id, right_id),
                offending_boundary_ordinals=(
                    boundary_ordinals[left_id],
                    boundary_ordinals[right_id],
                ),
                required_relation=(
                    "end_chronology_group_ordinal > start_chronology_group_ordinal"
                ),
            )


def parse_root_segmentation_output(
    raw: Mapping[str, Any],
    *,
    role: CandidateGraphRole,
    scope: AdaptiveAnalysisScope,
    bundles: Sequence[NativeOHLCVBundle],
    provider_name: str,
    provider_model: str | None,
    generated_at_utc: str,
) -> PriceSegmentationHypothesis:
    if not isinstance(raw, Mapping):
        raise RootStageParseError(
            RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
            "Segmentation provider output must be one object.",
            repairable=False,
            schema_conforming=False,
        )
    _reject_forbidden_provider_fields(raw)
    ordered_boundaries = _scope_boundaries(scope, bundles)
    boundaries = {item.boundary_id: item for item in ordered_boundaries}
    ordinals = {
        item.boundary_id: ordinal for ordinal, item in enumerate(ordered_boundaries)
    }
    chronology_groups = _boundary_chronology_groups(
        scope,
        bundles,
        ordered_boundaries,
    )
    observed_fields = set(raw)
    v4_fields = set(ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA["required"])
    v3_fields = set(ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA_V3["required"])
    legacy_v2 = observed_fields == {"segmentation_id", "segments"}
    legacy_v3 = observed_fields == v3_fields
    production_v4 = observed_fields == v4_fields
    if not (legacy_v2 or legacy_v3 or production_v4):
        raise RootStageParseError(
            RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
            "Segmentation provider fields must match a supported strict schema exactly.",
            repairable=False,
            schema_conforming=False,
        )

    segments: list[PriceSegmentCandidate] = []
    if production_v4:
        raw_steps = raw.get("steps")
        step_fields = {"end_boundary_id", "segment_kind"}
        if (
            isinstance(raw_steps, (str, bytes))
            or not isinstance(raw_steps, Sequence)
            or not raw_steps
            or any(
                not isinstance(item, Mapping) or set(item) != step_fields
                for item in raw_steps
            )
        ):
            raise RootStageParseError(
                RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                "Every Stage-A v4 step must match its strict schema.",
                repairable=False,
                schema_conforming=False,
            )
        segmentation_id = _text(raw["segmentation_id"], field_name="segmentation_id")
        step_ids = tuple(
            f"{segmentation_id}:step:{index:03d}"
            for index in range(1, len(raw_steps) + 1)
        )
        boundary_path = (
            scope.window_start.boundary_id,
            *(
                _text(item["end_boundary_id"], field_name="end_boundary_id")
                for item in raw_steps
            ),
        )
        _validate_root_boundary_path(
            boundary_path,
            scope=scope,
            boundaries=boundaries,
            boundary_ordinals=ordinals,
            chronology_groups=chronology_groups,
            affected_ids=step_ids,
        )
        for index, item in enumerate(raw_steps):
            start_id, end_id = boundary_path[index : index + 2]
            try:
                segments.append(
                    PriceSegmentCandidate(
                        segment_id=step_ids[index],
                        start_boundary=boundaries[start_id],
                        end_boundary=boundaries[end_id],
                        completion_state=(
                            SegmentCompletionState.ACTIVE
                            if index == len(raw_steps) - 1
                            else SegmentCompletionState.CLOSED
                        ),
                        segment_kind=item["segment_kind"],
                    )
                )
            except ValueError as exc:
                message = str(exc)
                schema_conforming = "is not a valid PriceSegmentKind" not in message
                raise RootStageParseError(
                    RootStageFailureCode.INVALID_ATOMIC_SWING_EXTREMA
                    if schema_conforming
                    else RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    message,
                    affected_id=step_ids[index],
                    offending_boundary_ids=(start_id, end_id),
                    offending_boundary_ordinals=(ordinals[start_id], ordinals[end_id]),
                    repairable=schema_conforming,
                    schema_conforming=schema_conforming,
                ) from exc
    elif legacy_v3:
        raw_path = raw.get("boundary_path")
        raw_segments = raw.get("segments")
        if (
            isinstance(raw_path, (str, bytes))
            or not isinstance(raw_path, Sequence)
            or len(raw_path) < 2
            or any(not isinstance(item, str) or not item.strip() for item in raw_path)
            or isinstance(raw_segments, (str, bytes))
            or not isinstance(raw_segments, Sequence)
        ):
            raise RootStageParseError(
                RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                "Historical v3 path data is malformed.",
                repairable=False,
                schema_conforming=False,
            )
        boundary_path = tuple(item.strip() for item in raw_path)
        descriptor_fields = {"segment_id", "completion_state", "segment_kind"}
        if any(
            not isinstance(item, Mapping) or set(item) != descriptor_fields
            for item in raw_segments
        ):
            raise RootStageParseError(
                RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                "Every historical v3 segment descriptor must match its strict schema.",
                repairable=False,
                schema_conforming=False,
            )
        if len(raw_segments) != len(boundary_path) - 1:
            expected_count = len(boundary_path) - 1
            observed_count = len(raw_segments)
            raise RootStageParseError(
                RootStageFailureCode.SEGMENT_DESCRIPTOR_COUNT_MISMATCH,
                (
                    "Historical v3 segment descriptor count mismatch: "
                    f"expected {expected_count}, received {observed_count}."
                ),
                affected_id=str(raw.get("segmentation_id") or "segmentation"),
                required_relation=(
                    f"segment_descriptor_count == {expected_count}"
                ),
            )
        _validate_root_boundary_path(
            boundary_path,
            scope=scope,
            boundaries=boundaries,
            boundary_ordinals=ordinals,
            chronology_groups=chronology_groups,
            affected_ids=tuple(str(item.get("segment_id") or "") for item in raw_segments),
        )
        for index, item in enumerate(raw_segments):
            start_id, end_id = boundary_path[index : index + 2]
            try:
                segments.append(
                    PriceSegmentCandidate(
                        segment_id=item["segment_id"],
                        start_boundary=boundaries[start_id],
                        end_boundary=boundaries[end_id],
                        completion_state=item["completion_state"],
                        segment_kind=item["segment_kind"],
                    )
                )
            except ValueError as exc:
                message = str(exc)
                schema_conforming = not any(
                    marker in message
                    for marker in (
                        "is not a valid SegmentCompletionState",
                        "is not a valid PriceSegmentKind",
                    )
                )
                raise RootStageParseError(
                    RootStageFailureCode.INVALID_ATOMIC_SWING_EXTREMA
                    if schema_conforming
                    else RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    message,
                    affected_id=str(item.get("segment_id") or f"segment-{index}"),
                    offending_boundary_ids=(start_id, end_id),
                    offending_boundary_ordinals=(ordinals[start_id], ordinals[end_id]),
                    repairable=schema_conforming,
                    schema_conforming=schema_conforming,
                ) from exc
    else:
        raw_segments = raw.get("segments")
        if isinstance(raw_segments, (str, bytes)) or not isinstance(raw_segments, Sequence):
            raise RootStageParseError(
                RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                "segments must be an array.",
                repairable=False,
                schema_conforming=False,
            )
        legacy_fields = {
            "segment_id",
            "start_boundary_id",
            "end_boundary_id",
            "completion_state",
        }
        typed_fields = {*legacy_fields, "segment_kind"}
        observed_shape: frozenset[str] | None = None
        for item in raw_segments:
            if not isinstance(item, Mapping) or frozenset(item) not in {
                frozenset(legacy_fields),
                frozenset(typed_fields),
            }:
                raise RootStageParseError(
                    RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    "Every legacy segmentation row must match its strict schema.",
                    repairable=False,
                    schema_conforming=False,
                )
            item_shape = frozenset(item)
            if observed_shape is None:
                observed_shape = item_shape
            elif observed_shape != item_shape:
                raise RootStageParseError(
                    RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    "Legacy and typed segment rows cannot be mixed.",
                    repairable=False,
                    schema_conforming=False,
                )
            start_id = _text(item["start_boundary_id"], field_name="start_boundary_id")
            end_id = _text(item["end_boundary_id"], field_name="end_boundary_id")
            if start_id not in boundaries or end_id not in boundaries:
                missing = tuple(
                    value for value in (start_id, end_id) if value not in boundaries
                )
                raise RootStageParseError(
                    RootStageFailureCode.UNAVAILABLE_BOUNDARY,
                    "Segmentation references an unavailable boundary.",
                    affected_id=str(item.get("segment_id") or "legacy-segment"),
                    offending_boundary_ids=missing,
                )
            _validate_root_boundary_path(
                (start_id, end_id),
                scope=scope,
                boundaries=boundaries,
                boundary_ordinals=ordinals,
                chronology_groups=chronology_groups,
                affected_ids=(str(item.get("segment_id") or "legacy-segment"),),
                require_scope_coverage=False,
            )
            try:
                segments.append(
                    PriceSegmentCandidate(
                        segment_id=item["segment_id"],
                        start_boundary=boundaries[start_id],
                        end_boundary=boundaries[end_id],
                        completion_state=item["completion_state"],
                        segment_kind=item.get("segment_kind"),
                    )
                )
            except ValueError as exc:
                code = (
                    RootStageFailureCode.NON_INCREASING_PRICE_SEGMENT_BOUNDARIES
                    if "end must follow" in str(exc)
                    else RootStageFailureCode.INVALID_ATOMIC_SWING_EXTREMA
                    if "atomic_swing" in str(exc) or "alternate" in str(exc)
                    else RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT
                )
                raise RootStageParseError(
                    code,
                    str(exc),
                    affected_id=str(item.get("segment_id") or "legacy-segment"),
                    offending_boundary_ids=(start_id, end_id),
                    offending_boundary_ordinals=(ordinals[start_id], ordinals[end_id]),
                    required_relation="end_boundary_ordinal > start_boundary_ordinal",
                    repairable=code is not RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    schema_conforming=code is not RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                ) from exc

    if not segments:
        raise RootStageParseError(
            RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
            "Segmentation cannot be empty.",
            repairable=False,
            schema_conforming=False,
        )
    if segments[0].start_boundary.boundary_id != scope.window_start.boundary_id:
        raise RootStageParseError(
            RootStageFailureCode.SEGMENTATION_DOES_NOT_BEGIN_AT_SCOPE,
            "Segmentation must begin at the analysis-scope boundary.",
        )
    if segments[-1].end_boundary.boundary_id != scope.as_of_observation.boundary_id:
        raise RootStageParseError(
            RootStageFailureCode.SEGMENTATION_DOES_NOT_END_AT_AS_OF,
            "Segmentation must end at the as-of observation.",
        )
    try:
        return PriceSegmentationHypothesis.create(
            segmentation_id=raw["segmentation_id"],
            role=CandidateGraphRole(role).value,
            analysis_scope_hash=scope.content_hash,
            segments=tuple(segments),
            provider=provider_name,
            model=provider_model,
            generated_at_utc=generated_at_utc,
        )
    except ValueError as exc:
        code = (
            RootStageFailureCode.DISCONNECTED_PRICE_SEGMENT_BOUNDARIES
            if "skip or disconnect" in str(exc)
            else RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT
        )
        raise RootStageParseError(
            code,
            str(exc),
            repairable=code is not RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
            schema_conforming=code is not RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
        ) from exc


def build_root_grouping_packet(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
    *,
    role: CandidateGraphRole,
    scope: AdaptiveAnalysisScope,
    segmentation: PriceSegmentationHypothesis,
    expected_degree: str | None = None,
) -> Mapping[str, Any]:
    """Build the blind Stage-B1 packet without Elliott family classifications."""

    if segmentation.analysis_scope_hash != scope.content_hash:
        raise ValueError("Grouping packet segmentation belongs to another scope.")
    if segmentation.role != CandidateGraphRole(role).value:
        raise ValueError("Grouping packet role does not match its segmentation.")
    if any(item.segment_kind is None for item in segmentation.segments):
        raise ValueError("Stage-B1 grouping requires typed Stage-A segments.")
    legacy = build_blind_root_packet(
        plan,
        bundles,
        role=role,
        expected_degree=expected_degree,
    )
    analysis_start = _utc(scope.window_start.timestamp_utc)
    as_of = _utc(scope.as_of_observation.timestamp_utc)
    price_evidence = []
    for row in legacy["price_evidence_only"]:
        price_evidence.append(
            {
                "bundle_identity": row["bundle_identity"],
                "native_candles": [
                    candle
                    for candle in row["native_candles"]
                    if analysis_start <= _utc(candle["timestamp_utc"]) <= as_of
                ],
                "pivot_catalog": [
                    pivot
                    for pivot in row["pivot_catalog"]
                    if analysis_start <= _utc(pivot["timestamp_utc"]) <= as_of
                ],
            }
        )
    return {
        "schema_version": ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA_VERSION,
        "candidate_role": CandidateGraphRole(role).value,
        "blind_isolation": legacy["blind_isolation"],
        "plan": legacy["plan"],
        "rules_pack": legacy["rules_pack"],
        "analysis_scope": scope.to_dict(),
        "segmentation": segmentation.to_dict(),
        "price_evidence": price_evidence,
        "instructions": {
            "stage": "root_structural_grouping",
            "candidate_only": True,
            "degree_labels_forbidden": True,
            "family_labels_forbidden": True,
            "verification_status_forbidden": True,
            "groups_may_span_one_or_more_contiguous_segments": True,
            "every_segment_grouped_or_explicitly_unresolved": True,
            "each_group_ordered_and_contiguous": True,
            "overlapping_groups_are_alternative_proposals_only": True,
            "selected_wave_nodes_must_form_a_non_overlapping_root_layer": True,
            "attempt_group_when_meaningful_swings_exist": True,
            "no_forced_group_when_price_structure_is_inadequate": True,
        },
    }


def parse_root_grouping_output(
    raw: Mapping[str, Any],
    *,
    role: CandidateGraphRole,
    segmentation: PriceSegmentationHypothesis,
    provider_name: str,
    provider_model: str | None,
    generated_at_utc: str,
) -> RootStructuralGroupingHypothesis:
    if not isinstance(raw, Mapping):
        raise ValueError("Root grouping provider output must be an object.")
    _reject_forbidden_provider_fields(raw)
    if set(raw) != set(ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA["required"]):
        raise ValueError("Root grouping fields must match the strict schema exactly.")
    segment_by_id = {item.segment_id: item for item in segmentation.segments}
    boundary_by_id = {
        boundary.boundary_id: boundary
        for segment in segmentation.segments
        for boundary in (segment.start_boundary, segment.end_boundary)
    }
    raw_groups = raw.get("groups")
    if isinstance(raw_groups, (str, bytes)) or not isinstance(raw_groups, Sequence):
        raise ValueError("groups must be an array.")
    required_group_fields = {
        "group_id",
        "source_segment_ids",
        "start_boundary_id",
        "end_boundary_id",
        "completion_state",
    }
    groups: list[StructuralSegmentGroup] = []
    for item in raw_groups:
        if not isinstance(item, Mapping) or set(item) != required_group_fields:
            raise ValueError("Every structural group must match the strict schema.")
        source_ids = item["source_segment_ids"]
        if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Sequence):
            raise ValueError("source_segment_ids must be an array.")
        source_ids = tuple(
            _text(value, field_name="source_segment_id") for value in source_ids
        )
        if not source_ids or any(value not in segment_by_id for value in source_ids):
            raise ValueError("Structural group references an unavailable source segment.")
        start_id = _text(item["start_boundary_id"], field_name="start_boundary_id")
        end_id = _text(item["end_boundary_id"], field_name="end_boundary_id")
        if start_id not in boundary_by_id or end_id not in boundary_by_id:
            raise ValueError("Structural group references an unavailable boundary.")
        groups.append(
            StructuralSegmentGroup(
                group_id=item["group_id"],
                source_segment_ids=source_ids,
                start_boundary=boundary_by_id[start_id],
                end_boundary=boundary_by_id[end_id],
                completion_state=item["completion_state"],
            )
        )
    unresolved = raw.get("unresolved_segment_ids")
    if isinstance(unresolved, (str, bytes)) or not isinstance(unresolved, Sequence):
        raise ValueError("unresolved_segment_ids must be an array.")
    grouping = RootStructuralGroupingHypothesis.create(
        grouping_id=raw["grouping_id"],
        role=CandidateGraphRole(role).value,
        segmentation_content_hash=segmentation.content_hash,
        groups=tuple(groups),
        unresolved_segment_ids=tuple(unresolved),
        provider=provider_name,
        model=provider_model,
        generated_at_utc=generated_at_utc,
    )
    grouping.validate_against(segmentation)
    return grouping


def build_candidate_scoped_evidence(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
    segmentation: PriceSegmentationHypothesis,
) -> tuple[CandidateScopedTechnicalEvidence, ...]:
    bundle = _scope_bundle(bundles)
    enriched = enrich_candles(
        [item.to_dict() for item in bundle.candles],
        features=plan.active_features,
    )
    evidence: list[CandidateScopedTechnicalEvidence] = []
    for segment in segmentation.segments:
        start = _utc(segment.start_boundary.timestamp_utc)
        end = _utc(segment.end_boundary.timestamp_utc)
        rows = [
            item
            for item in enriched
            if start <= _utc(str(item["timestamp_utc"])) <= end
        ]
        if not rows:
            continue
        summary = summarize_indicators(rows, features=plan.active_features)
        evidence.append(
            CandidateScopedTechnicalEvidence.create(
                evidence_id="",
                segment_id=segment.segment_id,
                timeframe=bundle.timeframe,
                start_timestamp_utc=segment.start_boundary.timestamp_utc,
                end_timestamp_utc=segment.end_boundary.timestamp_utc,
                active_features=plan.active_features,
                indicator_summary=summary,
                fibonacci_status="unavailable_without_comparable_candidate_wave",
                duration_bars=len(rows),
                channel_status=(
                    "calculated"
                    if summary.get("scale_comparison", {}).get("status") == "calculated"
                    else "unavailable"
                ),
                source_bundle_hash=bundle.content_hash,
            )
        )
    return tuple(evidence)


def build_root_classification_packet(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
    *,
    role: CandidateGraphRole,
    scope: AdaptiveAnalysisScope,
    segmentation: PriceSegmentationHypothesis,
    evidence: Sequence[CandidateScopedTechnicalEvidence],
    expected_degree: str | None = None,
) -> Mapping[str, Any]:
    legacy_price_packet = build_blind_root_packet(
        plan,
        bundles,
        role=role,
        expected_degree=expected_degree,
    )
    analysis_start = _utc(scope.window_start.timestamp_utc)
    as_of = _utc(scope.as_of_observation.timestamp_utc)
    context_start = _utc(plan.context_start_utc)
    price_evidence: list[dict[str, Any]] = []
    context_evidence: list[dict[str, Any]] = []
    for row in legacy_price_packet["price_evidence_only"]:
        identity = row["bundle_identity"]
        price_evidence.append(
            {
                "bundle_identity": identity,
                "native_candles": [
                    candle
                    for candle in row["native_candles"]
                    if analysis_start <= _utc(candle["timestamp_utc"]) <= as_of
                ],
                "pivot_catalog": [
                    pivot
                    for pivot in row["pivot_catalog"]
                    if analysis_start <= _utc(pivot["timestamp_utc"]) <= as_of
                ],
            }
        )
        context_evidence.append(
            {
                "bundle_identity": identity,
                "native_candles": [
                    candle
                    for candle in row["native_candles"]
                    if context_start
                    <= _utc(candle["timestamp_utc"])
                    < analysis_start
                ],
            }
        )
    return {
        "schema_version": ADAPTIVE_ROOT_CLASSIFICATION_OUTPUT_SCHEMA_VERSION,
        "candidate_role": CandidateGraphRole(role).value,
        "blind_isolation": legacy_price_packet["blind_isolation"],
        "plan": legacy_price_packet["plan"],
        "rules_pack": legacy_price_packet["rules_pack"],
        "analysis_scope": scope.to_dict(),
        "intervals": {
            "context_window": {
                "start_utc": plan.context_start_utc,
                "end_exclusive_utc": plan.analysis_start_utc,
                "mandatory_segmentation": False,
            },
            "requested_analysis_window": {
                "start_utc": plan.analysis_start_utc,
                "cutoff_utc": plan.analysis_cutoff_utc,
                "mandatory_segmentation": True,
            },
            "as_of_observation": scope.as_of_observation.to_dict(),
        },
        "segmentation": segmentation.to_dict(),
        "candidate_scoped_soft_evidence": [item.to_dict() for item in evidence],
        "context_price_evidence_only": context_evidence,
        "price_evidence": price_evidence,
        "instructions": {
            "stage": "segment_classification",
            "candidate_only": True,
            "catalog_pivots_only_for_wave_nodes": True,
            "wave_node_pivots_must_be_inside_source_segment": True,
            "allowed_root_degrees": legacy_price_packet["instructions"][
                "allowed_root_degrees"
            ],
            "root_degree_is_provisional": True,
            "analysis_scope_has_no_elliott_degree": True,
            "indicators_are_soft_ranking_evidence_only": True,
            "indicators_cannot_create_pivots_or_verify_waves": True,
            "unresolved_segments_may_have_no_wave_nodes": True,
            "verification_owner": "deterministic_subdivision_verifier",
            "context_candles_are_not_required_segments_or_elliott_nodes": True,
            "first_requested_segment_may_be_previous_parent_wave_tail": True,
        },
    }


def adaptive_root_classification_schema_for_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    schema = deepcopy(ADAPTIVE_ROOT_CLASSIFICATION_OUTPUT_SCHEMA)
    price_rows = packet.get("price_evidence")
    if isinstance(price_rows, (str, bytes)) or not isinstance(price_rows, Sequence):
        raise ValueError("Classification packet requires price evidence.")
    timeframes = sorted(
        {
            normalize_timeframe_name(item["bundle_identity"]["timeframe"])
            for item in price_rows
        }
    )
    allowed_degrees = packet.get("instructions", {}).get("allowed_root_degrees")
    if isinstance(allowed_degrees, (str, bytes)) or not isinstance(
        allowed_degrees, Sequence
    ):
        raise ValueError("Classification packet allowed root degrees are invalid.")
    allowed_degrees = list(allowed_degrees)
    derived = set(allowed_root_degrees_for_timeframes(timeframes))
    if (
        not allowed_degrees
        or len(set(allowed_degrees)) != len(allowed_degrees)
        or any(
            degree not in STANDARD_DEGREES
            or degree == "Unassigned"
            or degree not in derived
            for degree in allowed_degrees
        )
    ):
        raise ValueError("Classification packet allowed root degrees are invalid.")
    schema["properties"]["root_timeframe"] = {"type": "string", "enum": timeframes}
    node = schema["properties"]["nodes"]["items"]
    node["properties"]["timeframe"] = {"type": "string", "enum": timeframes}
    node["properties"]["degree"] = {"type": "string", "enum": allowed_degrees}
    segments = packet["segmentation"]["segments"]
    segment_ids = [item["segment_id"] for item in segments]
    pivot_rows = [
        pivot
        for evidence in price_rows
        for pivot in evidence.get("pivot_catalog", ())
    ]
    node_variants: list[dict[str, Any]] = []
    for segment in segments:
        start = _utc(segment["start_boundary"]["timestamp_utc"])
        end = _utc(segment["end_boundary"]["timestamp_utc"])
        local_pivot_ids = sorted(
            {
                _text(pivot["pivot_id"], field_name="pivot_id")
                for pivot in pivot_rows
                if start <= _utc(pivot["timestamp_utc"]) <= end
            }
        )
        if len(local_pivot_ids) < 2:
            continue
        variant = deepcopy(node)
        variant["properties"]["source_segment_id"] = {
            "type": "string",
            "enum": [segment["segment_id"]],
        }
        variant["properties"]["start_pivot_id"] = {
            "type": "string",
            "enum": local_pivot_ids,
        }
        variant["properties"]["end_pivot_id"] = {
            "type": "string",
            "enum": local_pivot_ids,
        }
        node_variants.append(variant)
    if node_variants:
        schema["properties"]["nodes"]["items"] = {"anyOf": node_variants}
    else:
        schema["properties"]["nodes"]["maxItems"] = 0
    classification = schema["properties"]["segment_classifications"]["items"]
    classification["properties"]["segment_id"] = {
        "type": "string",
        "enum": segment_ids,
    }
    evidence_ids = [
        item["evidence_id"] for item in packet["candidate_scoped_soft_evidence"]
    ]
    classification["properties"]["evidence_ids"]["items"] = {
        "type": "string",
        "enum": evidence_ids,
    }
    return schema


def build_root_group_classification_packet(
    plan: AdaptiveRecountPlan,
    bundles: Sequence[NativeOHLCVBundle],
    *,
    role: CandidateGraphRole,
    scope: AdaptiveAnalysisScope,
    segmentation: PriceSegmentationHypothesis,
    grouping: RootStructuralGroupingHypothesis,
    evidence: Sequence[CandidateScopedTechnicalEvidence],
    expected_degree: str | None = None,
) -> Mapping[str, Any]:
    """Build Stage-B2 input over frozen contiguous groups, not individual swings."""

    grouping.validate_against(segmentation)
    base = dict(
        build_root_classification_packet(
            plan,
            bundles,
            role=role,
            scope=scope,
            segmentation=segmentation,
            evidence=evidence,
            expected_degree=expected_degree,
        )
    )
    base["schema_version"] = ADAPTIVE_ROOT_GROUP_CLASSIFICATION_OUTPUT_SCHEMA_VERSION
    base["grouping"] = grouping.to_dict()
    base["instructions"] = {
        "stage": "group_family_classification",
        "candidate_only": True,
        "catalog_pivots_only_for_wave_nodes": True,
        "one_wave_node_per_classified_group": True,
        "wave_node_must_span_exact_group_boundaries": True,
        "wave_node_must_preserve_exact_ordered_source_segment_ids": True,
        "allowed_root_degrees": base["instructions"]["allowed_root_degrees"],
        "root_degree_is_provisional": True,
        "analysis_scope_has_no_elliott_degree": True,
        "indicators_are_soft_ranking_evidence_only": True,
        "indicators_cannot_create_pivots_or_verify_waves": True,
        "verification_owner": "deterministic_subdivision_verifier",
        "unresolved_group_may_have_no_wave_node": True,
    }
    return base


def adaptive_root_group_classification_schema_for_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    schema = deepcopy(ADAPTIVE_ROOT_GROUP_CLASSIFICATION_OUTPUT_SCHEMA)
    price_rows = packet.get("price_evidence")
    if isinstance(price_rows, (str, bytes)) or not isinstance(price_rows, Sequence):
        raise ValueError("Group-classification packet requires price evidence.")
    timeframes = sorted(
        {
            normalize_timeframe_name(item["bundle_identity"]["timeframe"])
            for item in price_rows
        }
    )
    allowed_degrees = packet.get("instructions", {}).get("allowed_root_degrees")
    if isinstance(allowed_degrees, (str, bytes)) or not isinstance(
        allowed_degrees, Sequence
    ):
        raise ValueError("Group-classification allowed root degrees are invalid.")
    allowed_degrees = list(allowed_degrees)
    derived = set(allowed_root_degrees_for_timeframes(timeframes))
    if (
        not allowed_degrees
        or len(set(allowed_degrees)) != len(allowed_degrees)
        or any(
            degree not in STANDARD_DEGREES
            or degree == "Unassigned"
            or degree not in derived
            for degree in allowed_degrees
        )
    ):
        raise ValueError("Group-classification allowed root degrees are invalid.")
    schema["properties"]["root_timeframe"] = {"type": "string", "enum": timeframes}
    node = schema["properties"]["nodes"]["items"]
    node["properties"]["timeframe"] = {"type": "string", "enum": timeframes}
    node["properties"]["degree"] = {"type": "string", "enum": allowed_degrees}
    pivot_by_id = {
        pivot["pivot_id"]: pivot
        for row in price_rows
        for pivot in row.get("pivot_catalog", ())
    }
    groups = packet["grouping"]["groups"]
    variants: list[dict[str, Any]] = []
    for group in groups:
        start_id = group["start_boundary"].get("source_pivot_id")
        end_id = group["end_boundary"].get("source_pivot_id")
        closed = group["completion_state"] == SegmentCompletionState.CLOSED.value
        if closed:
            if not start_id or not end_id or start_id not in pivot_by_id or end_id not in pivot_by_id:
                continue
            start_ids = [start_id]
            end_ids = [end_id]
        else:
            group_start = _utc(group["start_boundary"]["timestamp_utc"])
            group_end = _utc(group["end_boundary"]["timestamp_utc"])
            local_ids = sorted(
                pivot_id
                for pivot_id, pivot in pivot_by_id.items()
                if group_start <= _utc(pivot["timestamp_utc"]) < group_end
            )
            if len(local_ids) < 2:
                continue
            start_ids = local_ids
            end_ids = local_ids
        source_ids = list(group["source_segment_ids"])
        variant = deepcopy(node)
        variant["properties"]["source_group_id"] = {
            "type": "string",
            "enum": [group["group_id"]],
        }
        variant["properties"]["source_segment_ids"] = {
            "type": "array",
            "minItems": len(source_ids),
            "maxItems": len(source_ids),
            "items": {"type": "string", "enum": source_ids},
        }
        variant["properties"]["start_pivot_id"] = {
            "type": "string",
            "enum": start_ids,
        }
        variant["properties"]["end_pivot_id"] = {
            "type": "string",
            "enum": end_ids,
        }
        variant["properties"]["completion_state"] = {
            "type": "string",
            "enum": ["completed" if closed else "active"],
        }
        variants.append(variant)
    if variants:
        schema["properties"]["nodes"]["items"] = {"anyOf": variants}
        schema["properties"]["nodes"]["maxItems"] = len(variants)
    else:
        schema["properties"]["nodes"]["maxItems"] = 0
    classifications = schema["properties"]["group_classifications"]
    classifications["minItems"] = len(groups)
    classifications["maxItems"] = len(groups)
    classifications["items"]["properties"]["group_id"] = {
        "type": "string",
        "enum": [item["group_id"] for item in groups],
    }
    classifications["items"]["properties"]["evidence_ids"]["items"] = {
        "type": "string",
        "enum": [
            item["evidence_id"]
            for item in packet["candidate_scoped_soft_evidence"]
        ],
    }
    return schema


def _reject_forbidden_provider_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_PROVIDER_FIELDS & {str(key) for key in value}
        if forbidden:
            raise ValueError("Provider output contains forbidden fields: " + ", ".join(sorted(forbidden)))
        for item in value.values():
            _reject_forbidden_provider_fields(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_forbidden_provider_fields(item)


def _parse_invalidation(value: Any, *, catalog: Mapping[str, TypedPivot]) -> CandidateInvalidation:
    if not isinstance(value, Mapping):
        raise ValueError("invalidation must be an object.")
    invalidation = CandidateInvalidation.from_dict(value)
    if invalidation.source_pivot_id not in catalog:
        raise ValueError("Invalidation references a pivot outside the supplied catalog.")
    return invalidation


def _parse_optional_invalidation(
    value: Any,
    *,
    catalog: Mapping[str, TypedPivot],
) -> CandidateInvalidation | None:
    if value is None:
        return None
    return _parse_invalidation(value, catalog=catalog)


def _parse_diagonal_declaration(value: Any) -> DiagonalDeclaration | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("diagonal_declaration must be an object or null.")
    return DiagonalDeclaration.from_dict(value)


def _hypothesis_from_output(
    raw: Mapping[str, Any],
    *,
    plan: AdaptiveRecountPlan,
    role: CandidateGraphRole,
    bundles: Sequence[NativeOHLCVBundle],
    provider_name: str,
    provider_model: str | None,
    generated_at_utc: str,
) -> AdaptiveRootHypothesis:
    if not isinstance(raw, Mapping):
        raise ValueError("Root candidate provider output must be an object.")
    _reject_forbidden_provider_fields(raw)
    required = set(ADAPTIVE_ROOT_OUTPUT_SCHEMA["required"])
    if set(raw) != required:
        raise ValueError("Root candidate provider fields must match the strict schema exactly.")
    catalogs: dict[str, TypedPivot] = {}
    for bundle in bundles:
        window = native_window_from_bundle(bundle)
        for pivot in build_native_pivot_catalog(window):
            catalogs[pivot.pivot_id] = pivot
    raw_nodes = raw.get("nodes")
    if isinstance(raw_nodes, (str, bytes)) or not isinstance(raw_nodes, Sequence):
        raise ValueError("nodes must be an array.")
    nodes: list[AdaptiveWaveCandidate] = []
    allowed_node_fields = {
        "wave_id",
        "parent_wave_id",
        "degree",
        "sequence_position",
        "timeframe",
        "direction",
        "declared_family",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
        "child_wave_ids",
    }
    for index, item in enumerate(raw_nodes, start=1):
        if not isinstance(item, Mapping) or set(item) != allowed_node_fields:
            raise ValueError(f"nodes[{index}] fields must match the strict schema exactly.")
        start_id = _text(item["start_pivot_id"], field_name="start_pivot_id")
        end_id = _text(item["end_pivot_id"], field_name="end_pivot_id")
        if start_id not in catalogs or end_id not in catalogs:
            raise ValueError("Root candidate references an invented or unavailable pivot.")
        nodes.append(
            AdaptiveWaveCandidate(
                wave_id=item["wave_id"],
                parent_wave_id=item["parent_wave_id"],
                degree=item["degree"],
                sequence_position=item["sequence_position"],
                timeframe=item["timeframe"],
                direction=item["direction"],
                declared_family=item["declared_family"],
                completion_state=item["completion_state"],
                start_pivot=catalogs[start_id],
                end_pivot=catalogs[end_id],
                invalidation=_parse_invalidation(item["invalidation"], catalog=catalogs),
                child_wave_ids=tuple(item["child_wave_ids"]),
            )
        )
    return AdaptiveRootHypothesis.create(
        hypothesis_id=raw["hypothesis_id"],
        role=role,
        plan_id=plan.plan_id,
        plan_content_hash=plan.content_hash,
        symbol=plan.symbol,
        analysis_start_utc=plan.analysis_start_utc,
        analysis_cutoff_utc=plan.analysis_cutoff_utc,
        root_timeframe=raw["root_timeframe"],
        nodes=tuple(nodes),
        active_wave_id=raw["active_wave_id"],
        confirmation_conditions=tuple(raw["confirmation_conditions"]),
        unresolved_questions=tuple(raw["unresolved_questions"]),
        source_bundle_hashes=tuple(bundle.content_hash for bundle in bundles),
        rules_pack_content_hash=plan.rules_pack.content_hash,
        provider=provider_name,
        model=provider_model,
        generated_at_utc=generated_at_utc,
    )


def _staged_hypothesis_from_output(
    raw: Mapping[str, Any],
    *,
    plan: AdaptiveRecountPlan,
    role: CandidateGraphRole,
    bundles: Sequence[NativeOHLCVBundle],
    scope: AdaptiveAnalysisScope,
    segmentation: PriceSegmentationHypothesis,
    evidence: Sequence[CandidateScopedTechnicalEvidence],
    provider_name: str,
    provider_model: str | None,
    generated_at_utc: str,
) -> AdaptiveRootHypothesis:
    if not isinstance(raw, Mapping):
        raise ValueError("Root classification provider output must be an object.")
    _reject_forbidden_provider_fields(raw)
    required = set(ADAPTIVE_ROOT_CLASSIFICATION_OUTPUT_SCHEMA["required"])
    if set(raw) != required:
        raise ValueError("Root classification fields must match the strict schema exactly.")
    catalog: dict[str, TypedPivot] = {}
    for bundle in bundles:
        for pivot in build_native_pivot_catalog(native_window_from_bundle(bundle)):
            catalog[pivot.pivot_id] = pivot
    segment_by_id = {item.segment_id: item for item in segmentation.segments}
    raw_nodes = raw.get("nodes")
    if isinstance(raw_nodes, (str, bytes)) or not isinstance(raw_nodes, Sequence):
        raise ValueError("nodes must be an array.")
    allowed_node_fields = {
        "wave_id",
        "parent_wave_id",
        "degree",
        "sequence_position",
        "timeframe",
        "direction",
        "declared_family",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
        "child_wave_ids",
        "source_segment_id",
        "diagonal_declaration",
    }
    nodes: list[AdaptiveWaveCandidate] = []
    for index, item in enumerate(raw_nodes, start=1):
        if not isinstance(item, Mapping) or set(item) != allowed_node_fields:
            raise ValueError(f"nodes[{index}] fields must match the staged schema exactly.")
        start_id = _text(item["start_pivot_id"], field_name="start_pivot_id")
        end_id = _text(item["end_pivot_id"], field_name="end_pivot_id")
        if start_id not in catalog or end_id not in catalog:
            raise ValueError("Staged wave nodes require supplied catalog pivots.")
        segment_id = _text(item["source_segment_id"], field_name="source_segment_id")
        if segment_id not in segment_by_id:
            raise ValueError("Wave node references an unavailable price segment.")
        segment = segment_by_id[segment_id]
        start_time = _utc(catalog[start_id].timestamp_utc)
        end_time = _utc(catalog[end_id].timestamp_utc)
        if not (
            _utc(segment.start_boundary.timestamp_utc)
            <= start_time
            < end_time
            <= _utc(segment.end_boundary.timestamp_utc)
        ):
            raise ValueError("Wave node pivots must stay inside their provisional segment.")
        nodes.append(
            AdaptiveWaveCandidate(
                wave_id=item["wave_id"],
                parent_wave_id=item["parent_wave_id"],
                degree=item["degree"],
                sequence_position=item["sequence_position"],
                timeframe=item["timeframe"],
                direction=item["direction"],
                declared_family=item["declared_family"],
                completion_state=item["completion_state"],
                start_pivot=catalog[start_id],
                end_pivot=catalog[end_id],
                invalidation=_parse_optional_invalidation(
                    item["invalidation"], catalog=catalog
                ),
                child_wave_ids=tuple(item["child_wave_ids"]),
                source_segment_id=segment_id,
                diagonal_declaration=_parse_diagonal_declaration(
                    item["diagonal_declaration"]
                ),
            )
        )
    wave_ids = {item.wave_id for item in nodes}
    raw_classifications = raw.get("segment_classifications")
    if isinstance(raw_classifications, (str, bytes)) or not isinstance(
        raw_classifications, Sequence
    ):
        raise ValueError("segment_classifications must be an array.")
    classifications = tuple(
        SegmentClassification.from_dict(item) for item in raw_classifications
    )
    if {item.segment_id for item in classifications} != set(segment_by_id):
        raise ValueError("Every segment must be classified exactly once.")
    if len(classifications) != len(segment_by_id):
        raise ValueError("Segment classifications cannot duplicate a segment.")
    for classification in classifications:
        if any(wave_id not in wave_ids for wave_id in classification.wave_ids):
            raise ValueError("Segment classification references an unknown wave node.")
        actual = {
            item.wave_id for item in nodes if item.source_segment_id == classification.segment_id
        }
        if set(classification.wave_ids) != actual:
            raise ValueError("Segment classification wave IDs must match its staged nodes.")
        if classification.classification in {
            SegmentClassificationKind.UNRESOLVED,
            SegmentClassificationKind.PREVIOUS_PARENT_WAVE_TAIL,
            SegmentClassificationKind.DEGREE_TRANSITION,
        } and classification.wave_ids:
            raise ValueError("Context or unresolved segments cannot be forced into wave nodes.")
    return AdaptiveRootHypothesis.create(
        hypothesis_id=raw["hypothesis_id"],
        role=role,
        plan_id=plan.plan_id,
        plan_content_hash=plan.content_hash,
        symbol=plan.symbol,
        analysis_start_utc=plan.analysis_start_utc,
        analysis_cutoff_utc=plan.analysis_cutoff_utc,
        root_timeframe=raw["root_timeframe"],
        nodes=tuple(nodes),
        active_wave_id=raw["active_wave_id"],
        confirmation_conditions=tuple(raw["confirmation_conditions"]),
        unresolved_questions=tuple(raw["unresolved_questions"]),
        source_bundle_hashes=tuple(bundle.content_hash for bundle in bundles),
        rules_pack_content_hash=plan.rules_pack.content_hash,
        provider=provider_name,
        model=provider_model,
        generated_at_utc=generated_at_utc,
        analysis_scope=scope,
        segmentation=segmentation,
        segment_classifications=classifications,
        candidate_evidence=tuple(evidence),
    )


def _grouped_hypothesis_from_output(
    raw: Mapping[str, Any],
    *,
    plan: AdaptiveRecountPlan,
    role: CandidateGraphRole,
    bundles: Sequence[NativeOHLCVBundle],
    scope: AdaptiveAnalysisScope,
    segmentation: PriceSegmentationHypothesis,
    grouping: RootStructuralGroupingHypothesis,
    evidence: Sequence[CandidateScopedTechnicalEvidence],
    provider_name: str,
    provider_model: str | None,
    generated_at_utc: str,
) -> AdaptiveRootHypothesis:
    if not isinstance(raw, Mapping):
        raise ValueError("Root group-classification provider output must be an object.")
    _reject_forbidden_provider_fields(raw)
    required = set(ADAPTIVE_ROOT_GROUP_CLASSIFICATION_OUTPUT_SCHEMA["required"])
    if set(raw) != required:
        raise ValueError("Root group-classification fields must match the strict schema exactly.")
    grouping.validate_against(segmentation)
    catalog: dict[str, TypedPivot] = {}
    for bundle in bundles:
        for pivot in build_native_pivot_catalog(native_window_from_bundle(bundle)):
            catalog[pivot.pivot_id] = pivot
    group_by_id = {item.group_id: item for item in grouping.groups}
    raw_nodes = raw.get("nodes")
    if isinstance(raw_nodes, (str, bytes)) or not isinstance(raw_nodes, Sequence):
        raise ValueError("nodes must be an array.")
    allowed_node_fields = {
        "wave_id",
        "parent_wave_id",
        "degree",
        "sequence_position",
        "timeframe",
        "direction",
        "declared_family",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
        "child_wave_ids",
        "source_group_id",
        "source_segment_ids",
        "diagonal_declaration",
    }
    nodes: list[AdaptiveWaveCandidate] = []
    used_groups: set[str] = set()
    for index, item in enumerate(raw_nodes, start=1):
        if not isinstance(item, Mapping) or set(item) != allowed_node_fields:
            raise ValueError(f"nodes[{index}] fields must match the grouped schema exactly.")
        group_id = _text(item["source_group_id"], field_name="source_group_id")
        if group_id not in group_by_id or group_id in used_groups:
            raise ValueError("Each grouped wave node must reference one unique supplied group.")
        used_groups.add(group_id)
        group = group_by_id[group_id]
        source_ids = item["source_segment_ids"]
        if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Sequence):
            raise ValueError("source_segment_ids must be an array.")
        source_ids = tuple(source_ids)
        if source_ids != group.source_segment_ids:
            raise ValueError("Grouped wave source segments must exactly match group lineage.")
        start_id = _text(item["start_pivot_id"], field_name="start_pivot_id")
        end_id = _text(item["end_pivot_id"], field_name="end_pivot_id")
        if start_id not in catalog or end_id not in catalog:
            raise ValueError("Grouped wave nodes require supplied catalog pivots.")
        start_time = _utc(catalog[start_id].timestamp_utc)
        end_time = _utc(catalog[end_id].timestamp_utc)
        group_start = _utc(group.start_boundary.timestamp_utc)
        group_end = _utc(group.end_boundary.timestamp_utc)
        if not group_start <= start_time < end_time <= group_end:
            raise ValueError("Grouped wave pivots must remain inside their structural group.")
        completion = _text(item["completion_state"], field_name="completion_state").lower()
        if group.completion_state is SegmentCompletionState.CLOSED:
            if completion != "completed":
                raise ValueError("A closed structural group requires a completed candidate node.")
            if (
                group.start_boundary.source_pivot_id != start_id
                or group.end_boundary.source_pivot_id != end_id
            ):
                raise ValueError("Completed grouped waves must span exact group boundaries.")
        elif completion != "active":
            raise ValueError("An open structural group can only produce an active candidate node.")
        nodes.append(
            AdaptiveWaveCandidate(
                wave_id=item["wave_id"],
                parent_wave_id=item["parent_wave_id"],
                degree=item["degree"],
                sequence_position=item["sequence_position"],
                timeframe=item["timeframe"],
                direction=item["direction"],
                declared_family=item["declared_family"],
                completion_state=completion,
                start_pivot=catalog[start_id],
                end_pivot=catalog[end_id],
                invalidation=_parse_optional_invalidation(
                    item["invalidation"], catalog=catalog
                ),
                child_wave_ids=tuple(item["child_wave_ids"]),
                source_segment_ids=source_ids,
                source_group_id=group_id,
                source_grouping_hash=grouping.content_hash,
                diagonal_declaration=_parse_diagonal_declaration(
                    item["diagonal_declaration"]
                ),
            )
        )
    wave_ids = {item.wave_id for item in nodes}
    raw_classifications = raw.get("group_classifications")
    if isinstance(raw_classifications, (str, bytes)) or not isinstance(
        raw_classifications, Sequence
    ):
        raise ValueError("group_classifications must be an array.")
    classifications = tuple(
        GroupClassification.from_dict(item) for item in raw_classifications
    )
    if len(classifications) != len(group_by_id) or {
        item.group_id for item in classifications
    } != set(group_by_id):
        raise ValueError("Every structural group must be classified exactly once.")
    evidence_by_id = {item.evidence_id: item for item in evidence}
    nodes_by_group = {item.source_group_id: item for item in nodes}
    no_wave_kinds = {
        SegmentClassificationKind.UNRESOLVED,
        SegmentClassificationKind.PREVIOUS_PARENT_WAVE_TAIL,
        SegmentClassificationKind.DEGREE_TRANSITION,
        SegmentClassificationKind.UNFINISHED_STRUCTURE,
    }
    for classification in classifications:
        node = nodes_by_group.get(classification.group_id)
        if node is None:
            if classification.wave_id is not None or classification.classification not in no_wave_kinds:
                raise ValueError("An unclassified group must remain explicitly unresolved or contextual.")
        elif classification.wave_id != node.wave_id or classification.wave_id not in wave_ids:
            raise ValueError("Group classification references an unknown or mismatched wave node.")
        source_ids = set(group_by_id[classification.group_id].source_segment_ids)
        for evidence_id in classification.evidence_ids:
            evidence_item = evidence_by_id.get(evidence_id)
            if evidence_item is None or evidence_item.segment_id not in source_ids:
                raise ValueError("Group classification evidence lies outside its source segments.")
    return AdaptiveRootHypothesis.create(
        hypothesis_id=raw["hypothesis_id"],
        role=role,
        plan_id=plan.plan_id,
        plan_content_hash=plan.content_hash,
        symbol=plan.symbol,
        analysis_start_utc=plan.analysis_start_utc,
        analysis_cutoff_utc=plan.analysis_cutoff_utc,
        root_timeframe=raw["root_timeframe"],
        nodes=tuple(nodes),
        active_wave_id=raw["active_wave_id"],
        confirmation_conditions=tuple(raw["confirmation_conditions"]),
        unresolved_questions=tuple(raw["unresolved_questions"]),
        source_bundle_hashes=tuple(bundle.content_hash for bundle in bundles),
        rules_pack_content_hash=plan.rules_pack.content_hash,
        provider=provider_name,
        model=provider_model,
        generated_at_utc=generated_at_utc,
        analysis_scope=scope,
        segmentation=segmentation,
        segment_classifications=(),
        root_grouping=grouping,
        group_classifications=classifications,
        candidate_evidence=tuple(evidence),
    )


def _motive_price_screen(
    parent: AdaptiveWaveCandidate,
    children: Sequence[AdaptiveWaveCandidate],
) -> tuple[list[str], list[Mapping[str, Any]], list[str]]:
    if parent.declared_family not in {"impulse", "diagonal"} or len(children) != 5:
        return [], [], []
    ordered = tuple(children)
    points = (
        ordered[0].start_pivot.price,
        *(item.end_pivot.price for item in ordered),
    )
    calculation = calculate_motive_hard_rules(
        points,
        direction=parent.direction,
        family=parent.declared_family,
    )
    errors = [
        f"{parent.wave_id}: {item.rule.value}"
        for item in calculation.structural_invalidations
    ]
    calculations: list[Mapping[str, Any]] = [calculation.to_dict()]
    unresolved: list[str] = []
    if parent.declared_family == "diagonal":
        if parent.diagonal_declaration is None:
            unresolved.append(f"{parent.wave_id}: diagonal_declaration_required")
        else:
            try:
                geometry = calculate_diagonal_geometry(
                    points,
                    (
                        ordered[0].start_pivot.timestamp_utc,
                        *(item.end_pivot.timestamp_utc for item in ordered),
                    ),
                    direction=parent.direction,
                    declaration=parent.diagonal_declaration,
                    child_families=tuple(item.declared_family for item in ordered),
                )
            except (TypeError, ValueError) as exc:
                errors.append(f"{parent.wave_id}: diagonal_geometry_failure:{exc}")
            else:
                calculations.append(geometry.to_dict())
                errors.extend(
                    f"{parent.wave_id}: {item.rule.value}"
                    for item in geometry.structural_invalidations
                )
    return errors, calculations, unresolved


def screen_adaptive_hypothesis(
    hypothesis: AdaptiveRootHypothesis, *, screened_at_utc: str
) -> AdaptiveCandidateScreen:
    errors: list[str] = []
    unresolved: list[str] = []
    calculations: list[Mapping[str, Any]] = []
    nodes = {item.wave_id: item for item in hypothesis.nodes}
    if not nodes:
        unresolved.append("analysis_scope: no_elliott_wave_nodes_classified")
    actual_children: dict[str, list[AdaptiveWaveCandidate]] = {}
    for node in hypothesis.nodes:
        if node.parent_wave_id is not None:
            if node.parent_wave_id not in nodes:
                errors.append(f"{node.wave_id}: missing_parent")
            else:
                actual_children.setdefault(node.parent_wave_id, []).append(node)
                degree_errors = validate_direct_child_degree(
                    nodes[node.parent_wave_id].degree,
                    node.degree,
                )
                errors.extend(f"{node.wave_id}: {item}" for item in degree_errors)
        if _utc(node.start_pivot.timestamp_utc) < _utc(hypothesis.analysis_start_utc):
            # Context may precede the requested local structure, but it cannot be
            # silently labelled as part of that structure.
            unresolved.append(f"{node.wave_id}: pre_window_context_requires_explicit_scope")
    for parent in hypothesis.nodes:
        children = actual_children.get(parent.wave_id, [])
        listed = tuple(parent.child_wave_ids)
        actual_ids = tuple(item.wave_id for item in children)
        if set(listed) != set(actual_ids):
            errors.append(f"{parent.wave_id}: child_reference_mismatch")
        if not children:
            if parent.completion_state == "completed":
                unresolved.append(f"{parent.wave_id}: completed_parent_subdivision_unproven")
            continue
        ordered = sorted(children, key=lambda item: _utc(item.start_pivot.timestamp_utc))
        positions = tuple(item.sequence_position for item in ordered)
        expected = expected_child_positions(parent.declared_family, positions)
        if expected is None or positions != expected:
            errors.append(f"{parent.wave_id}: incomplete_or_invalid_child_sequence")
            continue
        if ordered[0].start_pivot.content_hash != parent.start_pivot.content_hash or ordered[-1].end_pivot.content_hash != parent.end_pivot.content_hash:
            errors.append(f"{parent.wave_id}: parent_child_boundary_mismatch")
        for left, right in zip(ordered, ordered[1:]):
            if left.end_pivot.content_hash != right.start_pivot.content_hash:
                errors.append(f"{parent.wave_id}: disconnected_child_boundaries")
        motive_errors, motive_calculations, motive_unresolved = _motive_price_screen(
            parent, ordered
        )
        errors.extend(motive_errors)
        calculations.extend(motive_calculations)
        unresolved.extend(motive_unresolved)
    if hypothesis.segmentation is not None:
        final_segment_id = hypothesis.segmentation.segments[-1].segment_id
        active_nodes = [
            item for item in hypothesis.nodes if item.completion_state == "active"
        ]
        if len(active_nodes) > 1:
            errors.append("analysis_scope: multiple_active_wave_nodes")
        if any(
            (
                item.source_segment_ids[-1]
                if item.source_segment_ids
                else item.source_segment_id
            )
            != final_segment_id
            for item in active_nodes
        ):
            errors.append("analysis_scope: only_final_segment_may_contain_active_wave")
        if hypothesis.active_wave_id is not None and hypothesis.active_wave_id not in {
            item.wave_id for item in active_nodes
        }:
            errors.append("analysis_scope: active_wave_id_not_active")
    return AdaptiveCandidateScreen.create(
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_hash=hypothesis.content_hash,
        status=(
            CandidateScreenStatus.REJECTED
            if errors
            else CandidateScreenStatus.SURVIVING_UNPROVEN
            if nodes
            else CandidateScreenStatus.NO_CANDIDATE
        ),
        hard_rule_errors=tuple(dict.fromkeys(errors)),
        unresolved_requirements=tuple(dict.fromkeys(unresolved)),
        screened_at_utc=screened_at_utc,
        hard_rule_calculations=tuple(calculations),
        boundary_validations=(),
    )


def adaptive_hypothesis_signature(hypothesis: AdaptiveRootHypothesis) -> str:
    node_indexes = {item.wave_id: index for index, item in enumerate(hypothesis.nodes)}
    segment_indexes = (
        {
            item.segment_id: index
            for index, item in enumerate(hypothesis.segmentation.segments)
        }
        if hypothesis.segmentation is not None
        else {}
    )
    group_indexes = (
        {
            item.group_id: index
            for index, item in enumerate(hypothesis.root_grouping.groups)
        }
        if hypothesis.root_grouping is not None
        else {}
    )

    def pivot_signature(pivot: TypedPivot) -> dict[str, Any]:
        return {
            "timestamp_utc": pivot.timestamp_utc,
            "price": pivot.price,
            "price_field": pivot.price_field.value,
            "pivot_type": pivot.pivot_type.value,
            "timeframe": pivot.timeframe,
        }

    nodes = [
            {
                # Generated identifiers are lineage handles, not structural
                # differences.  Compare the parent topology by node ordinal.
                "parent_index": (
                    node_indexes.get(item.parent_wave_id)
                    if item.parent_wave_id is not None
                    else None
                ),
                "degree": item.degree,
                "position": item.sequence_position,
                "family": item.declared_family,
                "completion": item.completion_state,
                "source_segment_index": segment_indexes.get(item.source_segment_id),
                "source_segment_indexes": [
                    segment_indexes.get(segment_id)
                    for segment_id in item.source_segment_ids
                ],
                "source_group_index": group_indexes.get(item.source_group_id),
                "diagonal_declaration": (
                    item.diagonal_declaration.to_dict()
                    if item.diagonal_declaration is not None
                    else None
                ),
                "start": pivot_signature(item.start_pivot),
                "end": pivot_signature(item.end_pivot),
                "invalidation": (
                    {
                        "threshold_price": item.invalidation.threshold_price,
                        "direction": item.invalidation.direction.value,
                        "evaluation_basis": item.invalidation.evaluation_basis.value,
                        # Pivot IDs come from the deterministic catalog and are
                        # meaningful lineage; only model-authored invalidation IDs
                        # are excluded from semantic distinctness.
                        "source_pivot_id": item.invalidation.source_pivot_id,
                    }
                    if item.invalidation is not None
                    else None
                ),
            }
            for item in hypothesis.nodes
        ]
    return canonical_sha256(
        {
            "nodes": nodes,
            "segmentation": (
                [
                    {
                        "start": item.start_boundary.boundary_id,
                        "end": item.end_boundary.boundary_id,
                        "completion": item.completion_state.value,
                    }
                    for item in hypothesis.segmentation.segments
                ]
                if hypothesis.segmentation is not None
                else None
            ),
            "segment_classifications": [
                {
                    "segment_id": item.segment_id,
                    "classification": item.classification.value,
                    "wave_ids": list(item.wave_ids),
                }
                for item in hypothesis.segment_classifications
            ],
            "root_grouping": (
                [
                    {
                        "source_segment_indexes": [
                            segment_indexes[segment_id]
                            for segment_id in item.source_segment_ids
                        ],
                        "start": item.start_boundary.boundary_id,
                        "end": item.end_boundary.boundary_id,
                        "completion": item.completion_state.value,
                    }
                    for item in hypothesis.root_grouping.groups
                ]
                if hypothesis.root_grouping is not None
                else None
            ),
            "group_classifications": [
                {
                    "group_index": group_indexes.get(item.group_id),
                    "classification": item.classification.value,
                    "wave_index": node_indexes.get(item.wave_id),
                }
                for item in hypothesis.group_classifications
            ],
            "active_node_index": (
                node_indexes.get(hypothesis.active_wave_id)
                if hypothesis.active_wave_id is not None
                else None
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class AdaptiveRootPair(_Contract):
    primary: AdaptiveRootHypothesis | None
    alternative: AdaptiveRootHypothesis | None
    primary_screen: AdaptiveCandidateScreen | None
    alternative_screen: AdaptiveCandidateScreen | None
    generated_at_utc: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.primary is None and self.alternative is None:
            raise ValueError("A partial root pair requires at least one generated role.")
        if (self.primary is None) != (self.primary_screen is None):
            raise ValueError("Primary hypothesis and screen must be present together.")
        if (self.alternative is None) != (self.alternative_screen is None):
            raise ValueError("Alternative hypothesis and screen must be present together.")
        if self.primary is not None and self.primary.role is not CandidateGraphRole.PRIMARY:
            raise ValueError("Primary root has the wrong role.")
        if self.alternative is not None:
            if self.alternative.role is not CandidateGraphRole.ALTERNATIVE:
                raise ValueError("Alternative root has the wrong role.")
            duplicate_no_candidates = (
                self.primary_screen is not None
                and self.primary_screen.status is CandidateScreenStatus.NO_CANDIDATE
                and self.alternative_screen is not None
                and self.alternative_screen.status is CandidateScreenStatus.NO_CANDIDATE
            )
            if (
                self.primary is not None
                and
                adaptive_hypothesis_signature(self.primary)
                == adaptive_hypothesis_signature(self.alternative)
                and not duplicate_no_candidates
            ):
                raise ValueError("Alternative root cannot duplicate the Primary graph.")
        object.__setattr__(self, "generated_at_utc", _utc_text(self.generated_at_utc, field_name="generated_at_utc"))
        if self.content_hash and self.content_hash != adaptive_root_pair_content_hash(self):
            raise ValueError("AdaptiveRootPair content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveRootPair":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=adaptive_root_pair_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveRootPair":
        raw = dict(value)
        raw["primary"] = (
            AdaptiveRootHypothesis.from_dict(raw["primary"])
            if raw.get("primary") is not None
            else None
        )
        raw["alternative"] = (
            AdaptiveRootHypothesis.from_dict(raw["alternative"])
            if raw.get("alternative") is not None
            else None
        )
        raw["primary_screen"] = (
            AdaptiveCandidateScreen.from_dict(raw["primary_screen"])
            if raw.get("primary_screen") is not None
            else None
        )
        raw["alternative_screen"] = (
            AdaptiveCandidateScreen.from_dict(raw["alternative_screen"])
            if raw.get("alternative_screen") is not None
            else None
        )
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_root_pair_content_hash(result):
            raise ValueError("AdaptiveRootPair content_hash does not match its payload.")
        return result


def adaptive_root_pair_content_hash(value: AdaptiveRootPair | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveRootPair) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class AdaptiveChildProofRecord(_Contract):
    role: CandidateGraphRole
    parent_wave_id: str
    planner_decision_hash: str
    data_manifest_hash: str
    generation_request_hash: str
    candidate_graph: CandidateChildGraph
    deterministic_result: SubdivisionVerifierResult
    generation_depth: int
    created_at_utc: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", CandidateGraphRole(self.role))
        object.__setattr__(self, "parent_wave_id", _text(self.parent_wave_id, field_name="parent_wave_id"))
        for name in ("planner_decision_hash", "data_manifest_hash", "generation_request_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if not isinstance(self.candidate_graph, CandidateChildGraph):
            raise TypeError("candidate_graph must be a CandidateChildGraph.")
        if not isinstance(self.deterministic_result, SubdivisionVerifierResult):
            raise TypeError("deterministic_result must be SubdivisionVerifierResult.")
        if self.deterministic_result.graph_content_hash != self.candidate_graph.content_hash:
            raise ValueError("Deterministic result does not belong to the candidate graph.")
        if not isinstance(self.generation_depth, int) or not 1 <= self.generation_depth <= 8:
            raise ValueError("generation_depth must be between one and eight.")
        object.__setattr__(self, "created_at_utc", _utc_text(self.created_at_utc, field_name="created_at_utc"))
        if self.content_hash and self.content_hash != adaptive_child_proof_record_content_hash(self):
            raise ValueError("AdaptiveChildProofRecord content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveChildProofRecord":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=adaptive_child_proof_record_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveChildProofRecord":
        raw = dict(value)
        raw["candidate_graph"] = CandidateChildGraph.from_dict(raw["candidate_graph"])
        raw["deterministic_result"] = SubdivisionVerifierResult.from_dict(
            raw["deterministic_result"]
        )
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_child_proof_record_content_hash(result):
            raise ValueError("AdaptiveChildProofRecord content_hash does not match its payload.")
        return result


def adaptive_child_proof_record_content_hash(value: AdaptiveChildProofRecord | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveChildProofRecord) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class AdaptiveChildReconstructionRejection(_Contract):
    """Minimal immutable ledger row for one rejected provider attempt."""

    role: CandidateGraphRole
    parent_wave_id: str
    attempt_number: int
    generation_request_hash: str
    returned_graph_signature: str
    excluded_graph_signature: str
    reason_code: str
    created_at_utc: str
    excluded_graph: ReconstructionExcludedGraph | None = None
    schema_version: str = ADAPTIVE_RECONSTRUCTION_REJECTION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", CandidateGraphRole(self.role))
        object.__setattr__(
            self,
            "parent_wave_id",
            _text(self.parent_wave_id, field_name="parent_wave_id"),
        )
        if not isinstance(self.attempt_number, int) or not 1 <= self.attempt_number <= 3:
            raise ValueError("Rejected reconstruction attempt must be attempt 1, 2, or 3.")
        for name in (
            "generation_request_hash",
            "returned_graph_signature",
            "excluded_graph_signature",
        ):
            object.__setattr__(
                self,
                name,
                _hash(getattr(self, name), field_name=name),
            )
        reason = _text(self.reason_code, field_name="reason_code")
        supported_reasons = {
            CandidateGenerationFailureCode.DUPLICATE_RECONSTRUCTION_CANDIDATE.value,
            CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT.value,
        }
        if reason not in supported_reasons:
            raise ValueError("Reconstruction rejection reason is unsupported.")
        object.__setattr__(self, "reason_code", reason)
        if self.excluded_graph is not None:
            graph = (
                self.excluded_graph
                if isinstance(self.excluded_graph, ReconstructionExcludedGraph)
                else ReconstructionExcludedGraph.from_dict(self.excluded_graph)
            )
            if graph.graph_signature != self.excluded_graph_signature:
                raise ValueError("Excluded graph signature does not match its typed exclusion.")
            object.__setattr__(self, "excluded_graph", graph)
        if (
            reason == CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT.value
            and self.excluded_graph is None
        ):
            raise ValueError("Malformed provider attempts require a typed exclusion.")
        object.__setattr__(
            self,
            "created_at_utc",
            _utc_text(self.created_at_utc, field_name="created_at_utc"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )
        if self.content_hash and self.content_hash != adaptive_reconstruction_rejection_content_hash(self):
            raise ValueError("AdaptiveChildReconstructionRejection content_hash does not match its payload.")

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.excluded_graph is None:
            payload.pop("excluded_graph", None)
        return payload

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveChildReconstructionRejection":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(
            result,
            content_hash=adaptive_reconstruction_rejection_content_hash(result),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveChildReconstructionRejection":
        raw = dict(value)
        if raw.get("excluded_graph") is not None:
            raw["excluded_graph"] = ReconstructionExcludedGraph.from_dict(
                raw["excluded_graph"]
            )
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_reconstruction_rejection_content_hash(result):
            raise ValueError("AdaptiveChildReconstructionRejection content_hash does not match its payload.")
        return result


def adaptive_reconstruction_rejection_content_hash(
    value: AdaptiveChildReconstructionRejection | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveChildReconstructionRejection) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class AdaptiveRecountTrace(_Contract):
    trace_id: str
    plan: AdaptiveRecountPlan
    status: AdaptiveRecountStatus
    stop_reason: AdaptiveStopReason
    planner_requests: tuple[AdaptiveTimeframeRequest, ...]
    planner_decisions: tuple[AdaptiveTimeframeDecision, ...]
    data_manifests: tuple[DataRequestManifest, ...]
    bundle_hashes: tuple[str, ...]
    bundle_validations: tuple[BundleValidationResult, ...]
    root_pair: AdaptiveRootPair | None
    child_proofs: tuple[AdaptiveChildProofRecord, ...]
    reconstruction_rejections: tuple[AdaptiveChildReconstructionRejection, ...]
    source_analysis_run_id: int | None
    authoritative_degree_resolution_id: int | None
    warnings: tuple[str, ...]
    created_at_utc: str
    updated_at_utc: str
    root_stage_failures: tuple[RootStageValidationFailure, ...] = ()
    root_stage_diagnostics: tuple[RootStageDiagnosticReference, ...] = ()
    root_stage_call_count: int = 0
    schema_version: str = ADAPTIVE_TRACE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_id", _text(self.trace_id, field_name="trace_id"))
        if not isinstance(self.plan, AdaptiveRecountPlan):
            raise TypeError("plan must be AdaptiveRecountPlan.")
        object.__setattr__(self, "status", AdaptiveRecountStatus(self.status))
        object.__setattr__(self, "stop_reason", AdaptiveStopReason(self.stop_reason))
        object.__setattr__(self, "bundle_hashes", tuple(_hash(item, field_name="bundle_hash") for item in self.bundle_hashes))
        if len(self.planner_requests) > self.plan.budgets.maximum_timeframe_requests:
            raise ValueError("Trace exceeds the timeframe request budget.")
        if len(self.child_proofs) > self.plan.budgets.maximum_candidate_graphs:
            raise ValueError("Trace exceeds the candidate graph budget.")
        object.__setattr__(self, "child_proofs", tuple(self.child_proofs))
        object.__setattr__(
            self,
            "reconstruction_rejections",
            tuple(self.reconstruction_rejections),
        )
        root_failures = tuple(self.root_stage_failures)
        if any(not isinstance(item, RootStageValidationFailure) for item in root_failures):
            raise TypeError("root_stage_failures must contain typed failure records.")
        object.__setattr__(self, "root_stage_failures", root_failures)
        diagnostics = tuple(self.root_stage_diagnostics)
        if any(not isinstance(item, RootStageDiagnosticReference) for item in diagnostics):
            raise TypeError("root_stage_diagnostics must contain typed references.")
        object.__setattr__(self, "root_stage_diagnostics", diagnostics)
        if (
            not isinstance(self.root_stage_call_count, int)
            or isinstance(self.root_stage_call_count, bool)
            or self.root_stage_call_count < 0
        ):
            raise ValueError("root_stage_call_count must be a nonnegative integer.")
        if (
            self.root_stage_call_count
            + len(self.child_proofs)
            + len(self.reconstruction_rejections)
            > self.plan.budgets.maximum_candidate_graphs
        ):
            raise ValueError("Trace exceeds the global candidate/model-attempt budget.")
        if len(self.root_stage_diagnostics) > self.plan.budgets.maximum_candidate_graphs:
            raise ValueError("Trace exceeds the root diagnostic-artifact budget.")
        if any(
            not isinstance(item, AdaptiveChildReconstructionRejection)
            for item in self.reconstruction_rejections
        ):
            raise TypeError("reconstruction_rejections must contain typed records.")
        for name in ("source_analysis_run_id", "authoritative_degree_resolution_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be a positive integer or null.")
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        for name in ("created_at_utc", "updated_at_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        if self.status is AdaptiveRecountStatus.FROZEN_BY_RESOLVE_DEGREES and self.authoritative_degree_resolution_id is None:
            raise ValueError("Frozen traces require the authoritative degree-resolution ID.")
        if self.content_hash and self.content_hash != adaptive_recount_trace_content_hash(self):
            raise ValueError("AdaptiveRecountTrace content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveRecountTrace":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("trace_id"):
            plan = values.get("plan")
            values["trace_id"] = "adaptive_trace_" + canonical_sha256(
                {"plan": plan.content_hash if isinstance(plan, AdaptiveRecountPlan) else "", "created": values.get("created_at_utc")}
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=adaptive_recount_trace_content_hash(result))

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if not self.reconstruction_rejections:
            # Preserve hash compatibility with traces written before the
            # append-only duplicate-attempt ledger existed.
            payload.pop("reconstruction_rejections", None)
        if (
            not self.root_stage_failures
            and not self.root_stage_diagnostics
            and self.root_stage_call_count == 0
        ):
            # Preserve exact hashes for traces created before root-stage repair
            # accounting became part of the append-only trace.
            payload.pop("root_stage_failures", None)
            payload.pop("root_stage_diagnostics", None)
            payload.pop("root_stage_call_count", None)
        return payload

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveRecountTrace":
        raw = dict(value)
        raw["plan"] = AdaptiveRecountPlan.from_dict(raw["plan"])
        raw["planner_requests"] = tuple(
            AdaptiveTimeframeRequest.from_dict(item) for item in raw.get("planner_requests", ())
        )
        raw["planner_decisions"] = tuple(
            AdaptiveTimeframeDecision.from_dict(item) for item in raw.get("planner_decisions", ())
        )
        raw["data_manifests"] = tuple(
            DataRequestManifest.from_dict(item) for item in raw.get("data_manifests", ())
        )
        raw["bundle_hashes"] = tuple(raw.get("bundle_hashes", ()))
        raw["bundle_validations"] = tuple(
            BundleValidationResult.from_dict(item) for item in raw.get("bundle_validations", ())
        )
        raw["root_pair"] = (
            AdaptiveRootPair.from_dict(raw["root_pair"])
            if raw.get("root_pair") is not None
            else None
        )
        raw["child_proofs"] = tuple(
            AdaptiveChildProofRecord.from_dict(item) for item in raw.get("child_proofs", ())
        )
        raw["reconstruction_rejections"] = tuple(
            AdaptiveChildReconstructionRejection.from_dict(item)
            for item in raw.get("reconstruction_rejections", ())
        )
        raw["root_stage_failures"] = tuple(
            RootStageValidationFailure.from_dict(item)
            for item in raw.get("root_stage_failures", ())
        )
        raw["root_stage_diagnostics"] = tuple(
            RootStageDiagnosticReference.from_dict(item)
            for item in raw.get("root_stage_diagnostics", ())
        )
        raw["root_stage_call_count"] = int(raw.get("root_stage_call_count", 0))
        raw["warnings"] = tuple(raw.get("warnings", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_recount_trace_content_hash(result):
            raise ValueError("AdaptiveRecountTrace content_hash does not match its payload.")
        return result


def adaptive_recount_trace_content_hash(value: AdaptiveRecountTrace | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveRecountTrace) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class _AdaptiveParentView:
    wave_id: str
    degree: str
    timeframe: str
    direction: str
    declared_family: str
    completion_state: str
    start_pivot: TypedPivot
    end_pivot: TypedPivot
    invalidation: CandidateInvalidation | None
    diagonal_declaration: DiagonalDeclaration | None
    generation_depth: int
    timeframe_use_purpose: TimeframeUsePurpose
    structural_parent_degree: str | None
    parent_evidence_timeframe: str | None


def _proof_records_for(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> tuple[AdaptiveChildProofRecord, ...]:
    return tuple(
        item
        for item in trace.child_proofs
        if item.role is role and item.parent_wave_id == parent_wave_id
    )


def _reconstruction_rejections_for(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> tuple[AdaptiveChildReconstructionRejection, ...]:
    return tuple(
        item
        for item in trace.reconstruction_rejections
        if item.role is role and item.parent_wave_id == parent_wave_id
    )


def _attempt_dispositions_for(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> tuple[AdaptiveChildAttemptDisposition, ...]:
    frozen_parent = AdaptiveRecountCoordinator._find_parent(
        trace,
        role=role,
        wave_id=parent_wave_id,
    )
    records = list(
        _proof_records_for(trace, role=role, parent_wave_id=parent_wave_id)
    )
    rejected_by_attempt = {
        item.attempt_number: item
        for item in _reconstruction_rejections_for(
            trace,
            role=role,
            parent_wave_id=parent_wave_id,
        )
    }
    total = len(records) + len(rejected_by_attempt)
    if rejected_by_attempt:
        total = max(total, max(rejected_by_attempt))
    dispositions: list[AdaptiveChildAttemptDisposition] = []
    proof_index = 0
    for attempt_number in range(1, total + 1):
        if attempt_number in rejected_by_attempt:
            rejection = rejected_by_attempt[attempt_number]
            dispositions.append(
                AdaptiveChildAttemptDisposition.DUPLICATE_RECONSTRUCTION_CANDIDATE
                if rejection.reason_code
                == CandidateGenerationFailureCode.DUPLICATE_RECONSTRUCTION_CANDIDATE.value
                else AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED
            )
        elif proof_index < len(records):
            record = records[proof_index]
            dispositions.append(
                AdaptiveChildAttemptDisposition.PARENT_FAMILY_RECLASSIFICATION_REQUIRED
                if record.candidate_graph.declared_family
                != frozen_parent.declared_family
                else _attempt_disposition(record)
            )
            proof_index += 1
    return tuple(dispositions)


def _proof_record_for(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> AdaptiveChildProofRecord | None:
    """Legacy convenience accessor returning the latest immutable attempt."""

    records = _proof_records_for(
        trace,
        role=role,
        parent_wave_id=parent_wave_id,
    )
    return records[-1] if records else None


def _attempt_disposition(
    record: AdaptiveChildProofRecord,
) -> AdaptiveChildAttemptDisposition:
    result = record.deterministic_result
    if result.status is SubdivisionVerificationStatus.VERIFIED:
        return AdaptiveChildAttemptDisposition.VERIFIED
    if result.status is SubdivisionVerificationStatus.NOT_COVERED:
        return AdaptiveChildAttemptDisposition.NOT_COVERED
    reasons = {item.value for item in result.reason_codes}
    if "pivot_not_true_extreme" in reasons:
        return AdaptiveChildAttemptDisposition.PIVOT_NOT_TRUE_EXTREME
    if "candidate_segmentation_failed" in reasons:
        return AdaptiveChildAttemptDisposition.CANDIDATE_SEGMENTATION_FAILED
    if "boundary_requires_reselection" in reasons:
        return AdaptiveChildAttemptDisposition.BOUNDARY_REQUIRES_RESELECTION
    if result.status is SubdivisionVerificationStatus.INCONSISTENT:
        return AdaptiveChildAttemptDisposition.CHILD_CANDIDATE_REJECTED
    return AdaptiveChildAttemptDisposition.UNPROVEN


def _admissible_parent_family_space(parent: _AdaptiveParentView) -> tuple[str, ...]:
    """Return the immutable family contract for same-parent reconstruction."""

    return (parent.declared_family,)


def _reconstruction_exclusion(
    record: AdaptiveChildProofRecord,
) -> ReconstructionExcludedGraph:
    graph = record.candidate_graph
    result = record.deterministic_result
    failed_checks = tuple(
        item for item in result.checks if item.required and item.passed is False
    )
    hard_rule_calculations = tuple(
        item.details
        for item in failed_checks
        if item.reason_code is VerificationReasonCode.HARD_PRICE_RULE_FAILURE
        and item.details
    )
    boundary_reason_codes = {
        VerificationReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH,
        VerificationReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS,
        VerificationReasonCode.BOUNDARY_REQUIRES_RESELECTION,
        VerificationReasonCode.PIVOT_NOT_TRUE_EXTREME,
        VerificationReasonCode.CANDIDATE_SEGMENTATION_FAILED,
    }
    failed_boundaries = tuple(
        {
            "check_id": item.check_id,
            "scope": item.scope,
            "reason_code": item.reason_code.value,
            "evidence_ids": item.evidence_ids,
            "details": item.details,
        }
        for item in failed_checks
        if item.reason_code in boundary_reason_codes
    )
    diagonal_reason_codes = {
        VerificationReasonCode.DIAGONAL_DECLARATION_REQUIRED,
        VerificationReasonCode.DIAGONAL_GEOMETRY_FAILURE,
        VerificationReasonCode.DIAGONAL_CHILD_FAMILY_INVALID,
        VerificationReasonCode.DIAGONAL_GEOMETRY_UNPROVEN,
    }
    failed_diagonal_checks = tuple(
        {
            "check_id": item.check_id,
            "scope": item.scope,
            "reason_code": item.reason_code.value,
            "details": item.details,
        }
        for item in failed_checks
        if item.reason_code in diagonal_reason_codes
    )
    rejected_diagonal = None
    if graph.declared_family == "diagonal" or failed_diagonal_checks:
        rejected_diagonal = {
            "declaration": (
                graph.diagonal_declaration.to_dict()
                if graph.diagonal_declaration is not None
                else None
            ),
            "failed_checks": failed_diagonal_checks,
        }
    failure_ids = tuple(
        dict.fromkeys(
            (
                *(item.value for item in result.reason_codes),
                *(item.reason_code.value for item in failed_checks),
            )
        )
    )
    return ReconstructionExcludedGraph.create(
        graph_signature=graph_signature(graph),
        pivot_sequence=(
            graph.children[0].start_pivot.pivot_id,
            *(item.end_pivot.pivot_id for item in graph.children),
        ),
        declared_family=graph.declared_family,
        child_family_sequence=tuple(
            item.declared_family for item in graph.children
        ),
        deterministic_failure_ids=failure_ids,
        hard_rule_calculations=hard_rule_calculations,
        failed_boundary_checks=failed_boundaries,
        rejected_diagonal=rejected_diagonal,
        verifier_result_hash=result.content_hash,
    )


def _child_reconstruction_context(
    *,
    parent: _AdaptiveParentView,
    records: Sequence[AdaptiveChildProofRecord],
    rejections: Sequence[AdaptiveChildReconstructionRejection],
    attempt_number: int,
) -> ChildReconstructionContext:
    exclusions = (
        *(_reconstruction_exclusion(item) for item in records),
        *(
            item.excluded_graph
            for item in rejections
            if item.excluded_graph is not None
        ),
    )
    return ChildReconstructionContext.create(
        attempt_number=attempt_number,
        excluded_graphs=tuple(exclusions),
        admissible_parent_families=_admissible_parent_family_space(parent),
        allowed_changes=RECONSTRUCTION_ALLOWED_CHANGES,
        planner_may_request_another_timeframe=True,
    )


def _selected_reconstruction_family(
    parent: _AdaptiveParentView,
    context: ChildReconstructionContext | None,
) -> str:
    if context is None:
        return parent.declared_family
    if context.admissible_parent_families != (parent.declared_family,):
        raise ValueError("parent_family_reclassification_required")
    if any(
        item.declared_family != parent.declared_family
        for item in context.excluded_graphs
    ):
        raise ValueError("parent_family_reclassification_required")
    return parent.declared_family


def _root_hypothesis_for_role(
    trace: AdaptiveRecountTrace,
    role: CandidateGraphRole,
) -> AdaptiveRootHypothesis | None:
    if trace.root_pair is None:
        return None
    return (
        trace.root_pair.primary
        if role is CandidateGraphRole.PRIMARY
        else trace.root_pair.alternative
    )


def _frozen_parent_rule_evidence(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Evaluate only already-frozen direct children of a root parent."""

    hypothesis = _root_hypothesis_for_role(trace, role)
    if hypothesis is None:
        return (), ()
    parent = next(
        (item for item in hypothesis.nodes if item.wave_id == parent_wave_id),
        None,
    )
    if parent is None or parent.completion_state != "completed":
        return (), ()
    direct_children = tuple(
        item for item in hypothesis.nodes if item.parent_wave_id == parent_wave_id
    )
    if not direct_children:
        return (), ()
    failed: list[str] = []
    hashes: list[str] = []
    if set(parent.child_wave_ids) != {item.wave_id for item in direct_children}:
        failed.append("child_reference_mismatch")
    by_position = {item.sequence_position: item for item in direct_children}
    expected = expected_child_positions(
        parent.declared_family,
        tuple(item.sequence_position for item in direct_children),
    )
    if expected is None or set(by_position) != set(expected):
        return tuple(dict.fromkeys(failed)), tuple(hashes)
    ordered = tuple(by_position[position] for position in expected)
    if any(item.completion_state != "completed" for item in ordered):
        return tuple(dict.fromkeys(failed)), tuple(hashes)
    if ordered[0].start_pivot.content_hash != parent.start_pivot.content_hash:
        failed.append("parent_child_boundary_mismatch")
    if ordered[-1].end_pivot.content_hash != parent.end_pivot.content_hash:
        failed.append("parent_child_boundary_mismatch")
    if any(
        left.end_pivot.content_hash != right.start_pivot.content_hash
        for left, right in zip(ordered, ordered[1:])
    ):
        failed.append("disconnected_child_boundaries")
    if parent.declared_family not in MOTIVE_FAMILIES or len(ordered) != 5:
        return tuple(dict.fromkeys(failed)), tuple(hashes)
    points = (
        ordered[0].start_pivot.price,
        *(item.end_pivot.price for item in ordered),
    )
    motive = calculate_motive_hard_rules(
        points,
        direction=parent.direction,
        family=parent.declared_family,
    )
    hashes.append(motive.content_hash)
    failed.extend(item.rule.value for item in motive.structural_invalidations)
    if parent.declared_family == "diagonal" and parent.diagonal_declaration is not None:
        try:
            geometry = calculate_diagonal_geometry(
                points,
                (
                    ordered[0].start_pivot.timestamp_utc,
                    *(item.end_pivot.timestamp_utc for item in ordered),
                ),
                direction=parent.direction,
                declaration=parent.diagonal_declaration,
                child_families=tuple(item.declared_family for item in ordered),
            )
        except (TypeError, ValueError):
            failed.append("diagonal_geometry_failure")
        else:
            hashes.append(geometry.content_hash)
            failed.extend(item.rule.value for item in geometry.structural_invalidations)
    return tuple(dict.fromkeys(failed)), tuple(dict.fromkeys(hashes))


def adaptive_parent_structural_assessment(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> ParentStructuralAssessment:
    """Derive parent inconsistency only from complete deterministic evidence."""

    role = CandidateGraphRole(role)
    parent = AdaptiveRecountCoordinator._find_parent(
        trace,
        role=role,
        wave_id=parent_wave_id,
    )
    records = _proof_records_for(
        trace,
        role=role,
        parent_wave_id=parent_wave_id,
    )
    failed_parent_rules, parent_calculation_hashes = _frozen_parent_rule_evidence(
        trace,
        role=role,
        parent_wave_id=parent_wave_id,
    )
    matching_records = tuple(
        item
        for item in records
        if item.candidate_graph.declared_family == parent.declared_family
    )
    parent_family_reclassification_required = len(matching_records) != len(records)
    missing_reasons = {
        VerificationReasonCode.REQUIRED_WINDOW_MISSING,
        VerificationReasonCode.REQUIRED_WINDOW_NOT_NATIVE,
        VerificationReasonCode.FULL_COVERAGE_MISSING,
        VerificationReasonCode.MINIMUM_NATIVE_BARS_NOT_MET,
    }
    data_integrity_reasons = {
        VerificationReasonCode.SOURCE_INTEGRITY_FAILED,
        VerificationReasonCode.REQUEST_HASH_MISMATCH,
        VerificationReasonCode.GRAPH_HASH_MISMATCH,
        VerificationReasonCode.GRAPH_REQUEST_MISMATCH,
        VerificationReasonCode.WINDOW_METADATA_INCOMPATIBLE,
        VerificationReasonCode.WINDOW_AFTER_CUTOFF,
        VerificationReasonCode.INCOMPLETE_CANDLE,
        *missing_reasons,
    }
    boundary_reasons = {
        VerificationReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH,
        VerificationReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS,
        VerificationReasonCode.BOUNDARY_REQUIRES_RESELECTION,
        VerificationReasonCode.PIVOT_NOT_TRUE_EXTREME,
        VerificationReasonCode.CANDIDATE_SEGMENTATION_FAILED,
    }
    all_reasons = {
        item
        for record in matching_records
        for item in record.deterministic_result.reason_codes
    }
    all_failed_checks = tuple(
        check
        for record in matching_records
        for check in record.deterministic_result.checks
        if check.required and check.passed is False
    )
    failed_reason_codes = all_reasons | {
        item.reason_code for item in all_failed_checks
    }
    missing_material_timeframe = any(
        record.deterministic_result.status
        is SubdivisionVerificationStatus.NOT_COVERED
        for record in matching_records
    ) or bool(failed_reason_codes & missing_reasons)
    unresolved_boundary = bool(failed_reason_codes & boundary_reasons)
    evidence_complete = bool(matching_records) and not bool(
        failed_reason_codes & data_integrity_reasons
    )
    calculation_hashes = list(parent_calculation_hashes)
    for check in all_failed_checks:
        content_hash = check.details.get("content_hash")
        if isinstance(content_hash, str) and len(content_hash) == 64:
            calculation_hashes.append(content_hash)
    return ParentStructuralAssessment.assess(
        parent_wave_id=parent_wave_id,
        failed_parent_rules=failed_parent_rules,
        child_attempts_examined=len(matching_records),
        admissible_family_space=_admissible_parent_family_space(parent),
        examined_family_space=tuple(
            dict.fromkeys(
                item.candidate_graph.declared_family for item in matching_records
            )
        ),
        # The active policy has no exhaustive catalog-backed family search. A
        # failed candidate graph cannot eliminate its family because another
        # same-family pivot decomposition may still be valid.
        deterministically_eliminated_families=(),
        evidence_complete=evidence_complete,
        unresolved_boundary_reselection=unresolved_boundary,
        missing_material_timeframe=missing_material_timeframe,
        parent_family_reclassification_required=(
            parent_family_reclassification_required
        ),
        calculation_hashes=tuple(dict.fromkeys(calculation_hashes)),
    )


def adaptive_parent_reconstruction_decision(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
) -> ChildReconstructionDecision:
    role = CandidateGraphRole(role)
    assessment = adaptive_parent_structural_assessment(
        trace,
        role=role,
        parent_wave_id=parent_wave_id,
    )
    return summarize_child_reconstruction(
        parent_wave_id=parent_wave_id,
        attempt_dispositions=_attempt_dispositions_for(
            trace,
            role=role,
            parent_wave_id=parent_wave_id,
        ),
        maximum_attempts=MAX_CHILD_RECOUNTS_PER_PARENT,
        parent_assessment=assessment,
    )


def effective_adaptive_proof_status(
    trace: AdaptiveRecountTrace,
    *,
    role: CandidateGraphRole,
    parent_wave_id: str,
    _visiting: frozenset[str] = frozenset(),
) -> SubdivisionVerificationStatus:
    """Propagate deterministic proof bottom-up across the adaptive graph chain."""

    key = f"{role.value}:{parent_wave_id}"
    if key in _visiting:
        return SubdivisionVerificationStatus.INCONSISTENT
    frozen_parent_family: str | None = None
    try:
        frozen_parent_family = AdaptiveRecountCoordinator._find_parent(
            trace,
            role=role,
            wave_id=parent_wave_id,
        ).declared_family
    except (AttributeError, TypeError, ValueError):
        # Lightweight historical test doubles may not carry a root hierarchy.
        # Full traces always resolve the frozen parent through this path.
        frozen_parent_family = None
    if isinstance(trace, AdaptiveRecountTrace):
        decision = adaptive_parent_reconstruction_decision(
            trace,
            role=role,
            parent_wave_id=parent_wave_id,
        )
        if decision.parent_status is AdaptiveParentProofDisposition.INCONSISTENT:
            return SubdivisionVerificationStatus.INCONSISTENT
    records = _proof_records_for(trace, role=role, parent_wave_id=parent_wave_id)
    if not records:
        return SubdivisionVerificationStatus.UNPROVEN
    attempt_statuses: list[SubdivisionVerificationStatus] = []
    for record in records:
        if (
            frozen_parent_family is not None
            and record.candidate_graph.declared_family != frozen_parent_family
        ):
            attempt_statuses.append(SubdivisionVerificationStatus.UNPROVEN)
            continue
        local = record.deterministic_result.status
        if local is SubdivisionVerificationStatus.VERIFIED:
            return local
        if local is SubdivisionVerificationStatus.NOT_COVERED:
            attempt_statuses.append(local)
            continue
        if local is SubdivisionVerificationStatus.INCONSISTENT:
            # The candidate child graph failed. This is not proof that the
            # immutable parent structure is impossible.
            attempt_statuses.append(SubdivisionVerificationStatus.UNPROVEN)
            continue
        terminal_reason = "terminal_degree_not_reached"
        blocking_checks = [
            check
            for check in record.deterministic_result.checks
            if check.required
            and check.reason_code.value != terminal_reason
            and check.passed is not True
        ]
        children = tuple(record.candidate_graph.children)
        if not children or any(item.completion_state != "completed" for item in children):
            attempt_statuses.append(SubdivisionVerificationStatus.UNPROVEN)
            continue
        child_statuses = tuple(
            effective_adaptive_proof_status(
                trace,
                role=role,
                parent_wave_id=child.child_id,
                _visiting=frozenset((*_visiting, key)),
            )
            for child in children
        )
        if any(item is SubdivisionVerificationStatus.NOT_COVERED for item in child_statuses):
            attempt_statuses.append(SubdivisionVerificationStatus.NOT_COVERED)
        elif blocking_checks or any(
            item is not SubdivisionVerificationStatus.VERIFIED for item in child_statuses
        ):
            attempt_statuses.append(SubdivisionVerificationStatus.UNPROVEN)
        elif child_statuses:
            return SubdivisionVerificationStatus.VERIFIED
    if attempt_statuses and all(
        item is SubdivisionVerificationStatus.NOT_COVERED for item in attempt_statuses
    ):
        return SubdivisionVerificationStatus.NOT_COVERED
    return SubdivisionVerificationStatus.UNPROVEN


def _strict_schema_errors(
    value: Any,
    schema: Mapping[str, Any],
    path: str = "output",
) -> list[str]:
    if "anyOf" in schema:
        variants = [
            _strict_schema_errors(value, variant, path)
            for variant in schema["anyOf"]
        ]
        return [] if any(not errors for errors in variants) else [f"{path} matches no allowed schema variant."]
    errors: list[str] = []
    expected_type = schema.get("type")
    types = tuple(expected_type) if isinstance(expected_type, list) else (expected_type,)

    def matches(name: Any) -> bool:
        if name == "object":
            return isinstance(value, Mapping)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        if name == "boolean":
            return isinstance(value, bool)
        if name == "null":
            return value is None
        return name is None

    if types != (None,) and not any(matches(name) for name in types):
        return [f"{path} has the wrong JSON type."]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} is outside its controlled enum.")
    if isinstance(value, str) and len(value) < int(schema.get("minLength", 0)):
        errors.append(f"{path} is shorter than minLength.")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for required in schema.get("required", ()):
            if required not in value:
                errors.append(f"{path} is missing {required}.")
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path} contains unknown field {key!r}.")
        for key, child in value.items():
            if key in properties:
                errors.extend(
                    _strict_schema_errors(child, properties[key], f"{path}.{key}")
                )
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path} has fewer than minItems.")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path} exceeds maxItems.")
        if "items" in schema:
            for index, child in enumerate(value):
                errors.extend(
                    _strict_schema_errors(child, schema["items"], f"{path}[{index}]")
                )
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} is below its minimum.")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} exceeds its maximum.")
    return errors


def _legacy_v2_segmentation_shape(raw: Any) -> bool:
    if not isinstance(raw, Mapping) or set(raw) != {"segmentation_id", "segments"}:
        return False
    rows = raw.get("segments")
    if not isinstance(rows, list) or not rows:
        return False
    legacy = {
        "segment_id",
        "start_boundary_id",
        "end_boundary_id",
        "completion_state",
    }
    typed = {*legacy, "segment_kind"}
    shapes = [set(item) for item in rows if isinstance(item, Mapping)]
    return len(shapes) == len(rows) and all(shape in (legacy, typed) for shape in shapes)


def _legacy_v3_segmentation_shape(raw: Any) -> bool:
    if not isinstance(raw, Mapping) or set(raw) != {
        "segmentation_id",
        "boundary_path",
        "segments",
    }:
        return False
    path = raw.get("boundary_path")
    rows = raw.get("segments")
    descriptor = {"segment_id", "completion_state", "segment_kind"}
    return (
        isinstance(path, list)
        and len(path) >= 2
        and all(isinstance(item, str) and item.strip() for item in path)
        and isinstance(rows, list)
        and bool(rows)
        and all(isinstance(item, Mapping) and set(item) == descriptor for item in rows)
    )


def _root_stage_schema_errors(
    stage: RootGenerationStage,
    raw: Any,
    schema: Mapping[str, Any],
) -> list[str]:
    if stage is RootGenerationStage.PRICE_SEGMENTATION and (
        _legacy_v2_segmentation_shape(raw) or _legacy_v3_segmentation_shape(raw)
    ):
        # Read-only/source-test compatibility. The active strict provider is
        # nevertheless bound to the v4 step schema before transport invocation.
        return []
    return _strict_schema_errors(raw, schema)


def root_stage_semantic_signature(
    stage: RootGenerationStage | str,
    raw: Mapping[str, Any],
) -> str:
    """Hash stage meaning while excluding generated cosmetic IDs."""

    stage = RootGenerationStage(stage)
    if stage is RootGenerationStage.PRICE_SEGMENTATION:
        if "steps" in raw:
            payload = {
                "steps": [
                    {
                        "end_boundary_id": item.get("end_boundary_id"),
                        "segment_kind": item.get("segment_kind"),
                    }
                    for item in raw.get("steps", ())
                    if isinstance(item, Mapping)
                ]
            }
        elif "boundary_path" in raw:
            payload = {
                "boundary_path": list(raw.get("boundary_path", ())),
                "segments": [
                    {
                        "segment_kind": item.get("segment_kind"),
                        "completion_state": item.get("completion_state"),
                    }
                    for item in raw.get("segments", ())
                    if isinstance(item, Mapping)
                ],
            }
        else:
            payload = {
                "segments": [
                    {
                        "start_boundary_id": item.get("start_boundary_id"),
                        "end_boundary_id": item.get("end_boundary_id"),
                        "segment_kind": item.get("segment_kind"),
                        "completion_state": item.get("completion_state"),
                    }
                    for item in raw.get("segments", ())
                    if isinstance(item, Mapping)
                ]
            }
    elif stage is RootGenerationStage.STRUCTURAL_GROUPING:
        payload = {
            "groups": [
                {
                    "source_segment_ids": list(item.get("source_segment_ids", ())),
                    "start_boundary_id": item.get("start_boundary_id"),
                    "end_boundary_id": item.get("end_boundary_id"),
                    "completion_state": item.get("completion_state"),
                }
                for item in raw.get("groups", ())
                if isinstance(item, Mapping)
            ],
            "unresolved_segment_ids": list(raw.get("unresolved_segment_ids", ())),
        }
    else:
        node_ids = [
            str(item.get("wave_id"))
            for item in raw.get("nodes", ())
            if isinstance(item, Mapping)
        ]
        node_index = {value: index for index, value in enumerate(node_ids)}
        payload = {
            "root_timeframe": raw.get("root_timeframe"),
            "nodes": [
                {
                    key: (
                        node_index.get(value)
                        if key == "parent_wave_id" and value is not None
                        else [node_index.get(item) for item in value]
                        if key == "child_wave_ids" and isinstance(value, list)
                        else value
                    )
                    for key, value in sorted(item.items())
                    if key not in {"wave_id"}
                }
                for item in raw.get("nodes", ())
                if isinstance(item, Mapping)
            ],
            "segment_classifications": [
                {
                    key: ([node_index.get(item) for item in value] if key == "wave_ids" else value)
                    for key, value in sorted(item.items())
                }
                for item in raw.get("segment_classifications", ())
                if isinstance(item, Mapping)
            ],
            "group_classifications": [
                {
                    key: (node_index.get(value) if key == "wave_id" and value is not None else value)
                    for key, value in sorted(item.items())
                }
                for item in raw.get("group_classifications", ())
                if isinstance(item, Mapping)
            ],
            "active_wave_index": node_index.get(raw.get("active_wave_id")),
            "confirmation_conditions": list(raw.get("confirmation_conditions", ())),
            "unresolved_questions": list(raw.get("unresolved_questions", ())),
        }
    return canonical_sha256(payload)


def _root_stage_parse_error(
    stage: RootGenerationStage,
    exc: Exception,
) -> RootStageParseError:
    if isinstance(exc, RootStageParseError):
        return exc
    message = str(exc) or type(exc).__name__
    lower = message.lower()
    if stage is RootGenerationStage.STRUCTURAL_GROUPING:
        code = (
            RootStageFailureCode.NON_CONTIGUOUS_STRUCTURAL_GROUP
            if "contiguous" in lower or "source order" in lower
            else RootStageFailureCode.GROUP_BOUNDARY_MISMATCH
        )
    elif stage is RootGenerationStage.FAMILY_CLASSIFICATION:
        code = (
            RootStageFailureCode.INVALID_ROOT_DEGREE
            if "degree" in lower or "timeframe" in lower
            else RootStageFailureCode.GROUPED_NODE_BOUNDARY_MISMATCH
        )
    else:
        code = RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT
    return RootStageParseError(
        code,
        message,
        repairable=True,
        schema_conforming=True,
    )


def _root_repair_context(
    *,
    role: CandidateGraphRole,
    stage: RootGenerationStage,
    attempt_number: int,
    failures: Sequence[RootStageValidationFailure],
    base_packet: Mapping[str, Any],
) -> RootStageRepairContext:
    available = tuple(
        RootStageBoundaryOrdinal(
            boundary_id=item["boundary_id"],
            boundary_ordinal=item["boundary_ordinal"],
        )
        for item in base_packet.get("available_boundaries", ())
        if isinstance(item, Mapping)
        and "boundary_id" in item
        and "boundary_ordinal" in item
    )
    scope = base_packet.get("analysis_scope")
    return RootStageRepairContext.create(
        role=role,
        stage=stage,
        attempt_number=attempt_number,
        excluded_output_hashes=tuple(
            dict.fromkeys(
                item.returned_structured_output_hash
                for item in failures
                if item.returned_structured_output_hash is not None
            )
        ),
        excluded_semantic_signatures=tuple(
            dict.fromkeys(
                item.semantic_signature
                for item in failures
                if item.semantic_signature is not None
            )
        ),
        deterministic_failures=tuple(
            RootStageRepairFact(
                error_code=item.error_code,
                affected_id=item.affected_id,
                offending_boundary_ids=item.offending_boundary_ids,
                offending_boundary_ordinals=item.offending_boundary_ordinals,
                required_relation=item.required_relation,
                chronology_group_id=item.chronology_group_id,
                chronology_group_ordinal=item.chronology_group_ordinal,
                source_bundle_hash=item.source_bundle_hash,
                source_bar_hash=item.source_bar_hash,
                shared_source_timestamp_utc=item.shared_source_timestamp_utc,
                intrabar_order_status=item.intrabar_order_status,
            )
            for item in failures
        ),
        available_boundary_ordinals=available,
        scope_start_boundary_id=(
            scope.get("window_start", {}).get("boundary_id")
            if isinstance(scope, Mapping)
            else None
        ),
        as_of_boundary_id=(
            scope.get("as_of_observation", {}).get("boundary_id")
            if isinstance(scope, Mapping)
            else None
        ),
        repair_instruction=ROOT_STAGE_REPAIR_INSTRUCTION,
    )


def write_immutable_root_stage_diagnostic(
    diagnostic: RejectedRootStageDiagnostic,
    output_directory: Path,
) -> RootStageDiagnosticReference:
    if diagnostic.content_hash != rejected_root_stage_diagnostic_content_hash(diagnostic):
        raise ValueError("Root-stage diagnostic hash is invalid.")
    directory = Path(output_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    failure = diagnostic.failure
    target = directory / (
        f"root_stage_rejection_{failure.role.value}_{failure.stage.value}_"
        f"attempt{failure.attempt_number}_{diagnostic.content_hash[:20]}.json"
    )
    if target.exists():
        raise FileExistsError(f"Immutable root-stage diagnostic already exists: {target}")
    payload = (canonical_forecast_json(diagnostic.to_dict()) + "\n").encode("utf-8")
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists():
        raise FileExistsError(f"Temporary root-stage diagnostic already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise FileExistsError(f"Immutable root-stage diagnostic already exists: {target}")
        temporary.rename(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return RootStageDiagnosticReference.create(
        role=failure.role,
        stage=failure.stage,
        attempt_number=failure.attempt_number,
        failure_content_hash=failure.content_hash,
        structured_output_hash=failure.returned_structured_output_hash,
        artifact_content_hash=diagnostic.content_hash,
        artifact_path=str(target),
        file_sha256=hashlib.sha256(payload).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class _RootStageRunResult:
    value: Any | None
    failures: tuple[RootStageValidationFailure, ...]
    diagnostics: tuple[RootStageDiagnosticReference, ...]
    calls_used: int


def _run_root_stage_with_repairs(
    *,
    role: CandidateGraphRole,
    stage: RootGenerationStage,
    base_packet: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    provider_call: Any,
    parser: Any,
    calls_already_used: int,
    maximum_calls: int,
    created_at_utc: str,
    diagnostic_output_directory: Path | None,
) -> _RootStageRunResult:
    failures: list[RootStageValidationFailure] = []
    diagnostics: list[RootStageDiagnosticReference] = []
    calls_used = 0
    schema_hash = canonical_sha256(output_schema)
    for attempt_number in range(1, MAX_ROOT_STAGE_ATTEMPTS + 1):
        packet = deepcopy(dict(base_packet))
        if attempt_number > 1:
            if not failures or not failures[-1].repairable:
                break
            packet["root_stage_repair_context"] = _root_repair_context(
                role=role,
                stage=stage,
                attempt_number=attempt_number,
                failures=failures,
                base_packet=base_packet,
            ).to_dict()
        packet_hash = canonical_sha256(packet)
        if calls_already_used + calls_used >= maximum_calls:
            failures.append(
                RootStageValidationFailure.create(
                    role=role,
                    stage=stage,
                    attempt_number=attempt_number,
                    packet_hash=packet_hash,
                    output_schema_hash=schema_hash,
                    returned_structured_output_hash=None,
                    semantic_signature=None,
                    error_code=RootStageFailureCode.ROOT_STAGE_ATTEMPT_BUDGET_EXHAUSTED,
                    affected_id=None,
                    offending_boundary_ids=(),
                    offending_boundary_ordinals=(),
                    required_relation=None,
                    deterministic_error_message="Global candidate/model-attempt budget is exhausted.",
                    repairable=False,
                    schema_conforming=False,
                    created_at_utc=created_at_utc,
                )
            )
            break
        raw = provider_call(packet, output_schema)
        calls_used += 1
        if not isinstance(raw, Mapping):
            raw_hash = canonical_sha256(_json_value(raw))
            failures.append(
                RootStageValidationFailure.create(
                    role=role,
                    stage=stage,
                    attempt_number=attempt_number,
                    packet_hash=packet_hash,
                    output_schema_hash=schema_hash,
                    returned_structured_output_hash=raw_hash,
                    semantic_signature=None,
                    error_code=RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    affected_id=None,
                    offending_boundary_ids=(),
                    offending_boundary_ordinals=(),
                    required_relation=None,
                    deterministic_error_message="Provider output must be one JSON object.",
                    repairable=False,
                    schema_conforming=False,
                    created_at_utc=created_at_utc,
                )
            )
            break
        raw = dict(raw)
        raw_hash = canonical_sha256(raw)
        schema_errors = _root_stage_schema_errors(stage, raw, output_schema)
        if schema_errors:
            failures.append(
                RootStageValidationFailure.create(
                    role=role,
                    stage=stage,
                    attempt_number=attempt_number,
                    packet_hash=packet_hash,
                    output_schema_hash=schema_hash,
                    returned_structured_output_hash=raw_hash,
                    semantic_signature=None,
                    error_code=RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    affected_id=None,
                    offending_boundary_ids=(),
                    offending_boundary_ordinals=(),
                    required_relation=None,
                    deterministic_error_message="; ".join(schema_errors),
                    repairable=False,
                    schema_conforming=False,
                    created_at_utc=created_at_utc,
                )
            )
            break
        signature = root_stage_semantic_signature(stage, raw)
        excluded = {
            item.semantic_signature
            for item in failures
            if item.semantic_signature is not None
        }
        parse_error: RootStageParseError | None = None
        if signature in excluded:
            parse_error = RootStageParseError(
                RootStageFailureCode.DUPLICATE_ROOT_STAGE_CANDIDATE,
                "Repair returned a previously rejected semantic root-stage candidate.",
                repairable=True,
                schema_conforming=True,
            )
        else:
            try:
                value = parser(raw)
            except Exception as exc:
                parse_error = _root_stage_parse_error(stage, exc)
            else:
                return _RootStageRunResult(
                    value=value,
                    failures=tuple(failures),
                    diagnostics=tuple(diagnostics),
                    calls_used=calls_used,
                )
        failure = RootStageValidationFailure.create(
            role=role,
            stage=stage,
            attempt_number=attempt_number,
            packet_hash=packet_hash,
            output_schema_hash=schema_hash,
            returned_structured_output_hash=raw_hash,
            semantic_signature=signature,
            error_code=parse_error.error_code,
            affected_id=parse_error.affected_id,
            offending_boundary_ids=parse_error.offending_boundary_ids,
            offending_boundary_ordinals=parse_error.offending_boundary_ordinals,
            required_relation=parse_error.required_relation,
            chronology_group_id=parse_error.chronology_group_id,
            chronology_group_ordinal=parse_error.chronology_group_ordinal,
            source_bundle_hash=parse_error.source_bundle_hash,
            source_bar_hash=parse_error.source_bar_hash,
            shared_source_timestamp_utc=parse_error.shared_source_timestamp_utc,
            intrabar_order_status=parse_error.intrabar_order_status,
            deterministic_error_message=str(parse_error),
            repairable=parse_error.repairable,
            schema_conforming=parse_error.schema_conforming,
            created_at_utc=created_at_utc,
        )
        failures.append(failure)
        if failure.schema_conforming:
            if diagnostic_output_directory is None:
                raise ValueError(
                    "diagnostic_output_directory is required to preserve rejected strict root output."
                )
            if len(diagnostics) >= maximum_calls:
                raise ValueError("Root-stage diagnostic artifact budget is exhausted.")
            diagnostic = RejectedRootStageDiagnostic.create(
                artifact_kind="non_evidence_root_stage_diagnostic",
                failure=failure,
                structured_output=raw,
                created_at_utc=created_at_utc,
            )
            diagnostics.append(
                write_immutable_root_stage_diagnostic(
                    diagnostic,
                    diagnostic_output_directory,
                )
            )
        if not failure.repairable:
            break
    return _RootStageRunResult(
        value=None,
        failures=tuple(failures),
        diagnostics=tuple(diagnostics),
        calls_used=calls_used,
    )


class AdaptiveRecountCoordinator:
    """Bounded coordinator for the pre-freeze adaptive technical loop."""

    def __init__(self) -> None:
        self.timeframe_planner = AdaptiveTimeframePlanner()
        self.child_generator = LowerTimeframeCandidateGenerator()
        self.subdivision_verifier = LowerTimeframeSubdivisionVerifier()

    def initial_trace(self, plan: AdaptiveRecountPlan) -> AdaptiveRecountTrace:
        return AdaptiveRecountTrace.create(
            trace_id="",
            plan=plan,
            status=AdaptiveRecountStatus.PLANNED,
            stop_reason=AdaptiveStopReason.NONE,
            planner_requests=(),
            planner_decisions=(),
            data_manifests=(),
            bundle_hashes=(),
            bundle_validations=(),
            root_pair=None,
            child_proofs=(),
            reconstruction_rejections=(),
            source_analysis_run_id=None,
            authoritative_degree_resolution_id=None,
            warnings=(),
            created_at_utc=plan.created_at_utc,
            updated_at_utc=plan.created_at_utc,
        )

    def plan_initial_data(
        self,
        trace: AdaptiveRecountTrace,
        *,
        created_at_utc: str,
        requested_timeframe: str | None = None,
        source_interface: str,
    ) -> AdaptiveRecountTrace:
        plan = trace.plan
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol=plan.symbol,
            parent_candidate_id="analysis_scope",
            parent_degree=None,
            parent_timeframe=None,
            parent_start_utc=plan.analysis_start_utc,
            parent_end_utc=plan.analysis_cutoff_utc,
            structural_question=StructuralQuestion.DISCOVER_ROOT_STRUCTURE,
            expected_child_degree=None,
            current_evidence_coverage=(),
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=plan.provider_capabilities.content_hash,
            provider_supported_intervals=plan.provider_capabilities.supported_timeframes,
            requested_timeframe=requested_timeframe,
            reason_code="initial_price_structure",
            cutoff_utc=plan.analysis_cutoff_utc,
            maximum_request_budget=plan.budgets.maximum_timeframe_requests,
            requests_already_used=len(trace.data_manifests),
            maximum_bar_budget=plan.budgets.maximum_bars,
            created_at_utc=created_at_utc,
            requested_data_start_utc=plan.context_start_utc,
            requested_data_end_utc=plan.analysis_cutoff_utc,
            policy_version=ROOT_TIMEFRAME_PLANNER_POLICY_VERSION,
        )
        decision = self.timeframe_planner.plan(request, plan.provider_capabilities)
        manifests = trace.data_manifests
        status = AdaptiveRecountStatus.NOT_COVERED
        stop = AdaptiveStopReason.MCP_UNAVAILABLE
        warnings = list(trace.warnings)
        if decision.status is TimeframePlanningStatus.PLANNED:
            manifest = manifest_from_planner(
                request,
                decision,
                plan.provider_capabilities,
                source_interface=source_interface,
            )
            manifests = (*manifests, manifest)
            status = AdaptiveRecountStatus.PENDING_DATA
            stop = AdaptiveStopReason.PENDING_NATIVE_DATA
        else:
            warnings.append(decision.stop_reason)
        return self._replace_trace(
            trace,
            planner_requests=(*trace.planner_requests, request),
            planner_decisions=(*trace.planner_decisions, decision),
            data_manifests=manifests,
            status=status,
            stop_reason=stop,
            warnings=tuple(warnings),
            updated_at_utc=created_at_utc,
        )

    def ingest_bundle(
        self,
        trace: AdaptiveRecountTrace,
        bundle: NativeOHLCVBundle,
        *,
        updated_at_utc: str,
    ) -> AdaptiveRecountTrace:
        manifest = next((item for item in trace.data_manifests if item.request_id == bundle.source_request_id), None)
        if manifest is None:
            raise ValueError("Bundle does not match a pending adaptive data request.")
        validation = validate_bundle_for_manifest(manifest, bundle, trace.plan.provider_capabilities)
        if bundle.content_hash in trace.bundle_hashes:
            raise ValueError("The immutable bundle has already been ingested.")
        bundle_hashes = (*trace.bundle_hashes, bundle.content_hash)
        validations = (*trace.bundle_validations, validation)
        planner_requests = trace.planner_requests
        planner_decisions = trace.planner_decisions
        manifests = trace.data_manifests
        warnings = list(trace.warnings)
        source_request = next(
            (
                item
                for item in trace.planner_requests
                if item.request_id == manifest.planner_request_id
            ),
            None,
        )
        prior_root_replan = any(
            item.reason_code == TimeframePlanningReason.ROOT_TIMEFRAME_REPLAN.value
            for item in trace.planner_requests
        )
        if (
            validation.status is BundleValidationStatus.VALID
            and trace.root_pair is None
            and manifest.structural_question
            is StructuralQuestion.DISCOVER_ROOT_STRUCTURE
            and source_request is not None
            and not prior_root_replan
        ):
            actual_bars = _actual_structural_bar_count(manifest, bundle)
            actual_distance = _focused_bar_distance(
                actual_bars,
                minimum=source_request.desired_native_bar_minimum,
                maximum=source_request.desired_native_bar_maximum,
            )
            if actual_distance >= _ROOT_REPLAN_MINIMUM_TARGET_DISTANCE:
                coverage = TimeframeEvidenceCoverage(
                    timeframe=bundle.timeframe,
                    first_timestamp_utc=bundle.actual_first_candle_utc,
                    last_completed_timestamp_utc=(
                        bundle.actual_final_completed_candle_utc
                    ),
                    native_bar_count=actual_bars,
                    full_parent_coverage=True,
                    native=bundle.native and not bundle.derived,
                    source_reference=bundle.bundle_id,
                )
                replan_request = AdaptiveTimeframeRequest.create(
                    request_id="",
                    symbol=trace.plan.symbol,
                    parent_candidate_id="analysis_scope",
                    parent_degree=None,
                    parent_timeframe=None,
                    parent_start_utc=manifest.structural_scoring_start_utc,
                    parent_end_utc=manifest.structural_scoring_end_utc,
                    structural_question=StructuralQuestion.DISCOVER_ROOT_STRUCTURE,
                    expected_child_degree=None,
                    current_evidence_coverage=(coverage,),
                    desired_native_bar_minimum=(
                        source_request.desired_native_bar_minimum
                    ),
                    desired_native_bar_maximum=(
                        source_request.desired_native_bar_maximum
                    ),
                    provider_capability_set_hash=(
                        trace.plan.provider_capabilities.content_hash
                    ),
                    provider_supported_intervals=(
                        trace.plan.provider_capabilities.supported_timeframes
                    ),
                    requested_timeframe=None,
                    reason_code=TimeframePlanningReason.ROOT_TIMEFRAME_REPLAN.value,
                    cutoff_utc=trace.plan.analysis_cutoff_utc,
                    maximum_request_budget=(
                        trace.plan.budgets.maximum_timeframe_requests
                    ),
                    requests_already_used=len(trace.data_manifests),
                    maximum_bar_budget=trace.plan.budgets.maximum_bars,
                    created_at_utc=updated_at_utc,
                    requested_data_start_utc=manifest.requested_start_utc,
                    requested_data_end_utc=manifest.requested_end_utc,
                    policy_version=ROOT_TIMEFRAME_PLANNER_POLICY_VERSION,
                )
                replan_decision = self.timeframe_planner.plan(
                    replan_request,
                    trace.plan.provider_capabilities,
                )
                replacement_distance = (
                    _focused_bar_distance(
                        replan_decision.estimated_native_bars,
                        minimum=replan_request.desired_native_bar_minimum,
                        maximum=replan_request.desired_native_bar_maximum,
                    )
                    if replan_decision.estimated_native_bars is not None
                    else actual_distance
                )
                if (
                    replan_decision.status is TimeframePlanningStatus.PLANNED
                    and actual_distance - replacement_distance
                    >= _ROOT_REPLAN_MINIMUM_IMPROVEMENT
                ):
                    planner_requests = (*planner_requests, replan_request)
                    planner_decisions = (*planner_decisions, replan_decision)
                    replan_manifest = manifest_from_planner(
                        replan_request,
                        replan_decision,
                        trace.plan.provider_capabilities,
                        source_interface=manifest.source_interface,
                    )
                    manifests = (*manifests, replan_manifest)
                    warnings.append(
                        "root_timeframe_replan: "
                        f"{bundle.timeframe} actual structural bars={actual_bars}; "
                        f"replacement={replan_manifest.canonical_timeframe} "
                        f"estimated bars={replan_decision.estimated_native_bars}."
                    )
        valid_request_ids = {
            item.request_id
            for item in validations
            if item.status is BundleValidationStatus.VALID
        }
        pending = [item for item in manifests if item.request_id not in valid_request_ids]
        status = (
            AdaptiveRecountStatus.RECURSIVE_PROOF_ACTIVE
            if trace.root_pair is not None and not pending
            else AdaptiveRecountStatus.READY_FOR_CANDIDATES
            if not pending
            else AdaptiveRecountStatus.PENDING_DATA
        )
        stop = AdaptiveStopReason.NONE if not pending else AdaptiveStopReason.PENDING_NATIVE_DATA
        if validation.status is BundleValidationStatus.NOT_COVERED:
            status = AdaptiveRecountStatus.NOT_COVERED
            stop = AdaptiveStopReason.LOWER_TIMEFRAME_NOT_COVERED
        elif validation.status is BundleValidationStatus.INVALID:
            status = AdaptiveRecountStatus.INCONSISTENT
        return self._replace_trace(
            trace,
            bundle_hashes=bundle_hashes,
            bundle_validations=validations,
            planner_requests=planner_requests,
            planner_decisions=planner_decisions,
            data_manifests=manifests,
            status=status,
            stop_reason=stop,
            warnings=tuple(warnings),
            updated_at_utc=updated_at_utc,
        )

    def generate_initial_candidates(
        self,
        trace: AdaptiveRecountTrace,
        bundles: Sequence[NativeOHLCVBundle],
        *,
        provider: AdaptiveRootCandidateProvider,
        allow_model_calls: bool = False,
        generated_at_utc: str,
        diagnostic_output_directory: Path | None = None,
    ) -> AdaptiveRecountTrace:
        if trace.status is not AdaptiveRecountStatus.READY_FOR_CANDIDATES:
            raise ValueError("Trace is not ready for initial candidates.")
        if not allow_model_calls:
            raise PermissionError("Initial candidate provider calls require explicit authorization.")
        if sum(len(bundle.candles) for bundle in bundles) > trace.plan.budgets.maximum_bars:
            raise ValueError("Bar budget exhausted before provider invocation.")
        supplied_hashes = {bundle.content_hash for bundle in bundles}
        if not supplied_hashes or not supplied_hashes.issubset(set(trace.bundle_hashes)):
            raise ValueError("Candidate generation requires previously validated bundle hashes.")
        for bundle in bundles:
            manifest = next(item for item in trace.data_manifests if item.request_id == bundle.source_request_id)
            validation = validate_bundle_for_manifest(manifest, bundle, trace.plan.provider_capabilities)
            if validation.status is not BundleValidationStatus.VALID:
                raise ValueError("No provider call is allowed after deterministic data preflight failure.")
        legacy_method = getattr(provider, "generate_root_candidate", None)
        segmentation_method = getattr(provider, "generate_root_segmentation", None)
        grouping_method = getattr(provider, "generate_root_grouping", None)
        group_classification_method = getattr(provider, "classify_root_groups", None)
        classification_method = getattr(provider, "classify_root_segments", None)
        grouped = (
            callable(segmentation_method)
            and callable(grouping_method)
            and callable(group_classification_method)
        )
        staged = callable(segmentation_method) and callable(classification_method)
        if not grouped and not staged and not callable(legacy_method):
            raise TypeError(
                "Root provider must implement the grouped, staged, or legacy root interface."
            )
        name = _text(getattr(provider, "name", None), field_name="provider.name")
        model = getattr(provider, "model", None)
        results: dict[CandidateGraphRole, AdaptiveRootHypothesis] = {}
        screens: dict[CandidateGraphRole, AdaptiveCandidateScreen] = {}
        root_failures: list[RootStageValidationFailure] = list(
            trace.root_stage_failures
        )
        root_diagnostics: list[RootStageDiagnosticReference] = list(
            trace.root_stage_diagnostics
        )
        new_root_calls = 0
        preexisting_budget_use = (
            trace.root_stage_call_count
            + len(trace.child_proofs)
            + len(trace.reconstruction_rejections)
        )
        root_manifests = {
            manifest.request_id: manifest
            for bundle in bundles
            for manifest in trace.data_manifests
            if manifest.request_id == bundle.source_request_id
        }
        if len(root_manifests) != len(bundles) or any(
            manifest.structural_question
            is not StructuralQuestion.DISCOVER_ROOT_STRUCTURE
            or manifest.parent_degree is not None
            or manifest.expected_child_degree is not None
            for manifest in root_manifests.values()
        ):
            raise ValueError(
                "Initial candidate bundles must come from degree-neutral root discovery."
            )
        scope = build_adaptive_analysis_scope(trace.plan, bundles)

        def absorb(stage_result: _RootStageRunResult) -> None:
            nonlocal new_root_calls
            new_root_calls += stage_result.calls_used
            root_failures.extend(stage_result.failures)
            root_diagnostics.extend(stage_result.diagnostics)

        def available_call_offset() -> int:
            return preexisting_budget_use + new_root_calls

        def run_role(role: CandidateGraphRole) -> None:
            nonlocal new_root_calls
            # Each role builds its packets from the immutable plan/bundles only.
            # No peer output or peer repair history is supplied.
            if grouped or staged:
                segmentation_packet = build_root_segmentation_packet(
                    trace.plan,
                    bundles,
                    role=role,
                    scope=scope,
                )
                segmentation_result = _run_root_stage_with_repairs(
                    role=role,
                    stage=RootGenerationStage.PRICE_SEGMENTATION,
                    base_packet=segmentation_packet,
                    output_schema=ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA,
                    provider_call=lambda packet, schema: segmentation_method(
                        role=role,
                        packet=packet,
                        output_schema=schema,
                    ),
                    parser=lambda raw: parse_root_segmentation_output(
                        raw,
                        role=role,
                        scope=scope,
                        bundles=bundles,
                        provider_name=name,
                        provider_model=model,
                        generated_at_utc=generated_at_utc,
                    ),
                    calls_already_used=available_call_offset(),
                    maximum_calls=trace.plan.budgets.maximum_candidate_graphs,
                    created_at_utc=generated_at_utc,
                    diagnostic_output_directory=diagnostic_output_directory,
                )
                absorb(segmentation_result)
                segmentation = segmentation_result.value
                if segmentation is None:
                    return
                evidence = build_candidate_scoped_evidence(
                    trace.plan,
                    bundles,
                    segmentation,
                )
                if grouped:
                    if any(
                        item.segment_kind is None for item in segmentation.segments
                    ):
                        root_failures.append(
                            RootStageValidationFailure.create(
                                role=role,
                                stage=RootGenerationStage.STRUCTURAL_GROUPING,
                                attempt_number=1,
                                packet_hash=canonical_sha256(
                                    {
                                        "segmentation": segmentation.content_hash,
                                        "role": role.value,
                                    }
                                ),
                                output_schema_hash=canonical_sha256(
                                    ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA
                                ),
                                returned_structured_output_hash=None,
                                semantic_signature=None,
                                error_code=RootStageFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                                affected_id=segmentation.segmentation_id,
                                offending_boundary_ids=(),
                                offending_boundary_ordinals=(),
                                required_relation=None,
                                deterministic_error_message=(
                                    "Grouped root generation requires typed Stage-A segments."
                                ),
                                repairable=False,
                                schema_conforming=False,
                                created_at_utc=generated_at_utc,
                            )
                        )
                        return
                    grouping_packet = build_root_grouping_packet(
                        trace.plan,
                        bundles,
                        role=role,
                        scope=scope,
                        segmentation=segmentation,
                    )
                    grouping_result = _run_root_stage_with_repairs(
                        role=role,
                        stage=RootGenerationStage.STRUCTURAL_GROUPING,
                        base_packet=grouping_packet,
                        output_schema=ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA,
                        provider_call=lambda packet, schema: grouping_method(
                            role=role,
                            packet=packet,
                            output_schema=schema,
                        ),
                        parser=lambda raw: parse_root_grouping_output(
                            raw,
                            role=role,
                            segmentation=segmentation,
                            provider_name=name,
                            provider_model=model,
                            generated_at_utc=generated_at_utc,
                        ),
                        calls_already_used=available_call_offset(),
                        maximum_calls=trace.plan.budgets.maximum_candidate_graphs,
                        created_at_utc=generated_at_utc,
                        diagnostic_output_directory=diagnostic_output_directory,
                    )
                    absorb(grouping_result)
                    grouping = grouping_result.value
                    if grouping is None:
                        return
                    classification_packet = build_root_group_classification_packet(
                        trace.plan,
                        bundles,
                        role=role,
                        scope=scope,
                        segmentation=segmentation,
                        grouping=grouping,
                        evidence=evidence,
                    )
                    classification_schema = (
                        adaptive_root_group_classification_schema_for_packet(
                            classification_packet
                        )
                    )
                    classification_result = _run_root_stage_with_repairs(
                        role=role,
                        stage=RootGenerationStage.FAMILY_CLASSIFICATION,
                        base_packet=classification_packet,
                        output_schema=classification_schema,
                        provider_call=lambda packet, schema: group_classification_method(
                            role=role,
                            packet=packet,
                            output_schema=schema,
                        ),
                        parser=lambda raw: _grouped_hypothesis_from_output(
                            raw,
                            plan=trace.plan,
                            role=role,
                            bundles=bundles,
                            scope=scope,
                            segmentation=segmentation,
                            grouping=grouping,
                            evidence=evidence,
                            provider_name=name,
                            provider_model=model,
                            generated_at_utc=generated_at_utc,
                        ),
                        calls_already_used=available_call_offset(),
                        maximum_calls=trace.plan.budgets.maximum_candidate_graphs,
                        created_at_utc=generated_at_utc,
                        diagnostic_output_directory=diagnostic_output_directory,
                    )
                else:
                    classification_packet = build_root_classification_packet(
                        trace.plan,
                        bundles,
                        role=role,
                        scope=scope,
                        segmentation=segmentation,
                        evidence=evidence,
                    )
                    classification_schema = (
                        adaptive_root_classification_schema_for_packet(
                            classification_packet
                        )
                    )
                    classification_result = _run_root_stage_with_repairs(
                        role=role,
                        stage=RootGenerationStage.FAMILY_CLASSIFICATION,
                        base_packet=classification_packet,
                        output_schema=classification_schema,
                        provider_call=lambda packet, schema: classification_method(
                            role=role,
                            packet=packet,
                            output_schema=schema,
                        ),
                        parser=lambda raw: _staged_hypothesis_from_output(
                            raw,
                            plan=trace.plan,
                            role=role,
                            bundles=bundles,
                            scope=scope,
                            segmentation=segmentation,
                            evidence=evidence,
                            provider_name=name,
                            provider_model=model,
                            generated_at_utc=generated_at_utc,
                        ),
                        calls_already_used=available_call_offset(),
                        maximum_calls=trace.plan.budgets.maximum_candidate_graphs,
                        created_at_utc=generated_at_utc,
                        diagnostic_output_directory=diagnostic_output_directory,
                    )
                absorb(classification_result)
                hypothesis = classification_result.value
                if hypothesis is None:
                    return
            else:
                if available_call_offset() >= trace.plan.budgets.maximum_candidate_graphs:
                    root_failures.append(
                        RootStageValidationFailure.create(
                            role=role,
                            stage=RootGenerationStage.FAMILY_CLASSIFICATION,
                            attempt_number=1,
                            packet_hash=canonical_sha256(
                                {"role": role.value, "legacy_root": True}
                            ),
                            output_schema_hash=canonical_sha256(
                                ADAPTIVE_ROOT_OUTPUT_SCHEMA
                            ),
                            returned_structured_output_hash=None,
                            semantic_signature=None,
                            error_code=RootStageFailureCode.ROOT_STAGE_ATTEMPT_BUDGET_EXHAUSTED,
                            affected_id=None,
                            offending_boundary_ids=(),
                            offending_boundary_ordinals=(),
                            required_relation=None,
                            deterministic_error_message=(
                                "Global candidate/model-attempt budget is exhausted."
                            ),
                            repairable=False,
                            schema_conforming=False,
                            created_at_utc=generated_at_utc,
                        )
                    )
                    return
                packet = build_blind_root_packet(
                    trace.plan,
                    bundles,
                    role=role,
                )
                output_schema = adaptive_root_output_schema_for_packet(packet)
                raw = legacy_method(
                    role=role,
                    packet=packet,
                    output_schema=output_schema,
                )
                new_root_calls += 1
                hypothesis = _hypothesis_from_output(
                    raw,
                    plan=trace.plan,
                    role=role,
                    bundles=bundles,
                    provider_name=name,
                    provider_model=model,
                    generated_at_utc=generated_at_utc,
                )
            results[role] = hypothesis
            screens[role] = screen_adaptive_hypothesis(
                hypothesis,
                screened_at_utc=generated_at_utc,
            )

        # The workflows execute serially for a deterministic provider-call
        # ledger, but their inputs, repairs, and outputs remain role-isolated.
        for role in (CandidateGraphRole.PRIMARY, CandidateGraphRole.ALTERNATIVE):
            run_role(role)

        primary = results.get(CandidateGraphRole.PRIMARY)
        alternative = results.get(CandidateGraphRole.ALTERNATIVE)
        primary_screen = screens.get(CandidateGraphRole.PRIMARY)
        alternative_screen = screens.get(CandidateGraphRole.ALTERNATIVE)
        duplicate_no_candidates = (
            primary_screen is not None
            and alternative_screen is not None
            and primary_screen.status is CandidateScreenStatus.NO_CANDIDATE
            and alternative_screen.status is CandidateScreenStatus.NO_CANDIDATE
        )
        if (
            primary is not None
            and alternative is not None
            and
            adaptive_hypothesis_signature(primary)
            == adaptive_hypothesis_signature(alternative)
            and not duplicate_no_candidates
        ):
            raise ValueError("Alternative candidate duplicates the Primary structure.")
        survivors = sum(
            item is not None
            and item.status is CandidateScreenStatus.SURVIVING_UNPROVEN
            for item in (primary_screen, alternative_screen)
        )
        root_pair = (
            AdaptiveRootPair.create(
                primary=primary,
                alternative=alternative,
                primary_screen=primary_screen,
                alternative_screen=alternative_screen,
                generated_at_utc=generated_at_utc,
            )
            if primary is not None or alternative is not None
            else None
        )
        both_roles_completed = primary is not None and alternative is not None
        if primary is None and alternative is None:
            status = AdaptiveRecountStatus.ROOT_CANDIDATE_GENERATION_FAILED
            stop = AdaptiveStopReason.ROOT_STAGE_CONTRACT_EXHAUSTED
        elif survivors:
            status = AdaptiveRecountStatus.CANDIDATES_FROZEN
            stop = (
                AdaptiveStopReason.TWO_VALID_CANDIDATES
                if survivors == 2
                else AdaptiveStopReason.ONE_VALID_CANDIDATE
            )
        elif both_roles_completed:
            status = AdaptiveRecountStatus.UNRESOLVED
            stop = AdaptiveStopReason.NO_VALID_CANDIDATE
        else:
            status = AdaptiveRecountStatus.UNRESOLVED
            stop = AdaptiveStopReason.ROOT_STAGE_CONTRACT_EXHAUSTED
        warnings = list(trace.warnings)
        for role in (CandidateGraphRole.PRIMARY, CandidateGraphRole.ALTERNATIVE):
            if role not in results:
                matching = [item for item in root_failures if item.role is role]
                if matching:
                    last = matching[-1]
                    warnings.append(
                        f"{role.value}_root_generation_failed:{last.stage.value}:"
                        f"{last.error_code.value}"
                    )
        return self._replace_trace(
            trace,
            root_pair=root_pair,
            status=status,
            stop_reason=stop,
            root_stage_failures=tuple(root_failures),
            root_stage_diagnostics=tuple(root_diagnostics),
            root_stage_call_count=trace.root_stage_call_count + new_root_calls,
            warnings=tuple(warnings),
            updated_at_utc=generated_at_utc,
        )

    def plan_parent_subdivision(
        self,
        trace: AdaptiveRecountTrace,
        *,
        role: CandidateGraphRole,
        parent_wave_id: str,
        bundles: Sequence[NativeOHLCVBundle],
        created_at_utc: str,
        requested_timeframe: str | None = None,
        source_interface: str,
    ) -> AdaptiveRecountTrace:
        if trace.root_pair is None:
            raise ValueError("Initial candidates must be frozen first.")
        role = CandidateGraphRole(role)
        parent = self._find_parent(trace, role=role, wave_id=parent_wave_id)
        child_degree = next_standard_degree(parent.degree)
        if child_degree is None or parent.degree == trace.plan.budgets.terminal_degree:
            raise ValueError("Parent has reached the terminal degree.")
        if parent.generation_depth >= trace.plan.budgets.maximum_recursion_depth:
            raise ValueError("Parent has reached the adaptive recursion-depth budget.")
        coverage = tuple(
            TimeframeEvidenceCoverage(
                timeframe=bundle.timeframe,
                first_timestamp_utc=bundle.actual_first_candle_utc,
                last_completed_timestamp_utc=bundle.actual_final_completed_candle_utc,
                native_bar_count=len(bundle.candles),
                full_parent_coverage=(
                    _utc(bundle.actual_first_candle_utc) <= _utc(parent.start_pivot.timestamp_utc)
                    and _utc(bundle.actual_final_completed_candle_utc) >= _utc(parent.end_pivot.timestamp_utc)
                ),
                native=bundle.native and not bundle.derived,
                source_reference=bundle.bundle_id,
                provider=bundle.provider,
                feed_identity=bundle.feed_identity,
                session_policy=bundle.session,
                adjustment_policy=bundle.price_adjustment,
                source_hash=bundle.content_hash,
                source_interface=bundle.source_interface,
                provider_symbol=bundle.provider_symbol,
                exchange=bundle.exchange,
                mic=bundle.mic,
                timezone=bundle.timezone,
                dividend_adjustment=bundle.dividend_adjustment,
                volume_adjustment=bundle.volume_adjustment,
                price_basis=bundle.price_basis,
                provider_interval_alias=bundle.provider_interval_alias,
                timestamp_semantics=bundle.timestamp_semantics,
                provider_bar_alignment=bundle.provider_bar_alignment,
                feed_family_hash=bundle.feed_family.feed_family_hash,
                stream_hash=bundle.stream_identity.stream_hash,
            )
            for bundle in bundles
        )
        request = AdaptiveTimeframeRequest.create(
            request_id="",
            symbol=trace.plan.symbol,
            parent_candidate_id=f"{role.value}:{parent.wave_id}",
            parent_degree=parent.degree,
            parent_timeframe=parent.timeframe,
            parent_start_utc=parent.start_pivot.timestamp_utc,
            parent_end_utc=parent.end_pivot.timestamp_utc,
            structural_question=StructuralQuestion.PROVE_PARENT_SUBDIVISION,
            expected_child_degree=child_degree,
            current_evidence_coverage=coverage,
            desired_native_bar_minimum=100,
            desired_native_bar_maximum=140,
            provider_capability_set_hash=trace.plan.provider_capabilities.content_hash,
            provider_supported_intervals=trace.plan.provider_capabilities.supported_timeframes,
            requested_timeframe=requested_timeframe,
            reason_code=f"prove:{role.value}:{parent.wave_id}",
            cutoff_utc=trace.plan.analysis_cutoff_utc,
            maximum_request_budget=trace.plan.budgets.maximum_timeframe_requests,
            requests_already_used=len(trace.data_manifests),
            maximum_bar_budget=trace.plan.budgets.maximum_bars,
            created_at_utc=created_at_utc,
            requested_data_start_utc=parent.start_pivot.timestamp_utc,
            requested_data_end_utc=parent.end_pivot.timestamp_utc,
        )
        decision = self.timeframe_planner.plan(request, trace.plan.provider_capabilities)
        manifests = trace.data_manifests
        status = AdaptiveRecountStatus.RECURSIVE_PROOF_ACTIVE
        stop = AdaptiveStopReason.NONE
        warnings = list(trace.warnings)
        if decision.status is TimeframePlanningStatus.PLANNED:
            manifests = (*manifests, manifest_from_planner(request, decision, trace.plan.provider_capabilities, source_interface=source_interface))
            status = AdaptiveRecountStatus.PENDING_DATA
            stop = AdaptiveStopReason.PENDING_NATIVE_DATA
        elif decision.status is TimeframePlanningStatus.NOT_COVERED:
            status = AdaptiveRecountStatus.NOT_COVERED
            stop = AdaptiveStopReason.LOWER_TIMEFRAME_NOT_COVERED
            warnings.append(decision.stop_reason)
        elif decision.status is TimeframePlanningStatus.BUDGET_EXHAUSTED:
            status = AdaptiveRecountStatus.UNRESOLVED
            stop = (
                AdaptiveStopReason.BAR_BUDGET_EXHAUSTED
                if decision.reason_code is TimeframePlanningReason.BAR_BUDGET_EXCEEDED
                else AdaptiveStopReason.REQUEST_BUDGET_EXHAUSTED
            )
            warnings.append(decision.stop_reason)
        elif decision.status is TimeframePlanningStatus.CONFIGURATION_ERROR:
            status = AdaptiveRecountStatus.UNRESOLVED
            stop = AdaptiveStopReason.PROOF_POLICY_CONFIGURATION_ERROR
            warnings.append(decision.stop_reason)
        else:
            warnings.append(decision.stop_reason)
        return self._replace_trace(
            trace,
            planner_requests=(*trace.planner_requests, request),
            planner_decisions=(*trace.planner_decisions, decision),
            data_manifests=manifests,
            status=status,
            stop_reason=stop,
            warnings=tuple(warnings),
            updated_at_utc=created_at_utc,
        )

    def generate_and_verify_child_graph(
        self,
        trace: AdaptiveRecountTrace,
        *,
        role: CandidateGraphRole,
        parent_wave_id: str,
        bundle: NativeOHLCVBundle,
        graph_provider: GraphGenerationProvider,
        generation_policy: CandidateGenerationPolicy,
        verifier_policy: SubdivisionVerifierPolicy,
        allow_model_call: bool = False,
        created_at_utc: str,
    ) -> AdaptiveRecountTrace:
        if trace.root_pair is None:
            raise ValueError("Initial candidate pair is missing.")
        if (
            len(trace.child_proofs) + len(trace.reconstruction_rejections)
            >= trace.plan.budgets.maximum_candidate_graphs
        ):
            raise ValueError("Candidate graph budget exhausted.")
        role = CandidateGraphRole(role)
        parent = self._find_parent(trace, role=role, wave_id=parent_wave_id)
        existing_attempts = _proof_records_for(
            trace,
            role=role,
            parent_wave_id=parent_wave_id,
        )
        existing_rejections = _reconstruction_rejections_for(
            trace,
            role=role,
            parent_wave_id=parent_wave_id,
        )
        attempts_used = len(existing_attempts) + len(existing_rejections)
        if attempts_used >= MAX_CHILD_RECOUNTS_PER_PARENT:
            raise ValueError("Bounded child reconstruction attempts are exhausted.")
        pre_generation_decision = adaptive_parent_reconstruction_decision(
            trace,
            role=role,
            parent_wave_id=parent_wave_id,
        )
        if (
            pre_generation_decision.parent_status
            is AdaptiveParentProofDisposition.INCONSISTENT
        ):
            return self._replace_trace(
                trace,
                status=AdaptiveRecountStatus.INCONSISTENT,
                stop_reason=AdaptiveStopReason.NO_VALID_CANDIDATE,
                warnings=(*trace.warnings, "parent_structurally_inconsistent"),
                updated_at_utc=created_at_utc,
            )
        if (
            "parent_family_reclassification_required"
            in pre_generation_decision.reason_codes
        ):
            return self._replace_trace(
                trace,
                status=AdaptiveRecountStatus.UNRESOLVED,
                stop_reason=AdaptiveStopReason.RECOUNT_REQUIRED,
                warnings=(
                    *trace.warnings,
                    "parent_family_reclassification_required",
                ),
                updated_at_utc=created_at_utc,
            )
        if parent.generation_depth >= trace.plan.budgets.maximum_recursion_depth:
            raise ValueError("Recursion-depth budget exhausted before graph generation.")
        manifest = next((item for item in trace.data_manifests if item.request_id == bundle.source_request_id), None)
        if manifest is None:
            raise ValueError("Bundle is not linked to the trace.")
        validation = validate_bundle_for_manifest(manifest, bundle, trace.plan.provider_capabilities)
        if validation.status is not BundleValidationStatus.VALID:
            raise ValueError("No graph provider call is permitted after data preflight failure.")
        window = native_window_from_bundle(bundle)
        catalog = build_native_pivot_catalog(window)
        if len(catalog) > trace.plan.budgets.maximum_pivots:
            raise ValueError("Pivot budget exhausted.")
        start = self._lineage_pivot(parent.start_pivot, catalog)
        end = self._lineage_pivot(parent.end_pivot, catalog)
        if start is None or end is None:
            raise ValueError("Exact lower-timeframe boundary lineage is not covered.")
        attempt_number = attempts_used + 1
        reconstruction_context = (
            _child_reconstruction_context(
                parent=parent,
                records=existing_attempts,
                rejections=existing_rejections,
                attempt_number=attempt_number,
            )
            if attempt_number > 1
            else None
        )
        _selected_reconstruction_family(
            parent,
            reconstruction_context,
        )
        parent_snapshot = ParentCandidateSnapshot.create(
            parent_candidate_id=(
                f"{role.value}:{parent.wave_id}:{bundle.timeframe}:"
                f"attempt-{attempt_number}"
            ),
            source_wave_id=parent.wave_id,
            source_analysis_run_id=None,
            source_analysis_run_hash=None,
            source_degree_resolution_id=None,
            source_degree_resolution_hash=None,
            degree=parent.degree,
            timeframe=parent.timeframe,
            direction=parent.direction,
            declared_family=parent.declared_family,
            completion_state=parent.completion_state,
            start_pivot=start,
            end_pivot=end,
            invalidation=parent.invalidation,
            dataset_cutoff=_bundle_dataset_cutoff(bundle),
            generation_depth=parent.generation_depth,
            timeframe_use_purpose=parent.timeframe_use_purpose,
            structural_parent_degree=parent.structural_parent_degree,
            parent_evidence_timeframe=parent.parent_evidence_timeframe,
            diagonal_declaration=(
                parent.diagonal_declaration
                if parent.declared_family == "diagonal"
                else None
            ),
        )
        child_degree = next_standard_degree(parent.degree)
        if child_degree is None:
            raise ValueError("Parent degree has no child degree.")
        generation_request = LowerTimeframeCandidateGenerationRequest.create(
            request_id="",
            parent_candidate=parent_snapshot,
            analysis_cutoff_utc=trace.plan.analysis_cutoff_utc,
            target_child_degree=child_degree,
            target_timeframe=bundle.timeframe,
            native_ohlcv_scope=NativeOHLCVScope.create(required_window=window, supporting_windows=()),
            pivot_catalog=catalog,
            proof_scope=CandidateProofScope(
                target_timeframe=bundle.timeframe,
                verified_to_timeframe=None,
                terminal_degree=generation_policy.terminal_degree,
                coverage_limitations=(),
            ),
            generation_policy=generation_policy,
            shadow_mode=True,
            created_at_utc=created_at_utc,
            reconstruction_context=reconstruction_context,
        )
        try:
            graph = self.child_generator.generate_one(
                generation_request,
                role=role,
                provider=graph_provider,
                allow_model_call=allow_model_call,
                generated_at_utc=created_at_utc,
            )
        except CandidateGenerationError as exc:
            exclusion = exc.failure.reconstruction_exclusion
            if (
                exc.failure.code
                is CandidateGenerationFailureCode.DUPLICATE_RECONSTRUCTION_CANDIDATE
            ):
                detail_values = {
                    key: value
                    for item in exc.failure.details
                    if "=" in item
                    for key, value in (item.split("=", 1),)
                }
                returned_signature = detail_values.get("returned_graph_signature")
                excluded_signature = detail_values.get("excluded_graph_signature")
                if returned_signature is None or excluded_signature is None:
                    raise ValueError("Duplicate reconstruction failure omitted its signatures.") from exc
            elif (
                exc.failure.code
                in {
                    CandidateGenerationFailureCode.MALFORMED_STRUCTURED_OUTPUT,
                    CandidateGenerationFailureCode.INTRABAR_ORDER_RESOLUTION_REQUIRED,
                }
                and exclusion is not None
            ):
                returned_signature = exclusion.graph_signature
                excluded_signature = exclusion.graph_signature
            else:
                raise
            rejection = AdaptiveChildReconstructionRejection.create(
                role=role,
                parent_wave_id=parent.wave_id,
                attempt_number=attempt_number,
                generation_request_hash=generation_request.content_hash,
                returned_graph_signature=returned_signature,
                excluded_graph_signature=excluded_signature,
                reason_code=exc.failure.code.value,
                created_at_utc=created_at_utc,
                excluded_graph=exclusion,
            )
            rejected_trace = self._replace_trace(
                trace,
                reconstruction_rejections=(
                    *trace.reconstruction_rejections,
                    rejection,
                ),
                updated_at_utc=created_at_utc,
            )
            rejection_decision = adaptive_parent_reconstruction_decision(
                rejected_trace,
                role=role,
                parent_wave_id=parent_wave_id,
            )
            exhausted = (
                rejection_decision.search_outcome
                is AdaptiveSearchOutcome.CANDIDATE_SEARCH_EXHAUSTED
            )
            return self._replace_trace(
                rejected_trace,
                status=(
                    AdaptiveRecountStatus.UNRESOLVED
                    if exhausted
                    else AdaptiveRecountStatus.RECURSIVE_PROOF_ACTIVE
                ),
                stop_reason=(
                    AdaptiveStopReason.CANDIDATE_SEARCH_EXHAUSTED
                    if exhausted
                    else AdaptiveStopReason.RECOUNT_REQUIRED
                ),
                updated_at_utc=created_at_utc,
            )
        verification_request = build_subdivision_verification_request(
            generation_request,
            graph,
            verifier_policy=verifier_policy,
            shadow_mode=True,
            created_at_utc=created_at_utc,
        )
        result = self.subdivision_verifier.verify(verification_request, shadow_mode=True)
        planner_request_id = next(
            (
                item.request_id
                for item in reversed(trace.planner_requests)
                if item.parent_candidate_id == f"{role.value}:{parent.wave_id}"
                and item.requested_timeframe in {None, bundle.timeframe}
            ),
            None,
        )
        if planner_request_id is None:
            raise ValueError("No deterministic planner request authorizes this parent/timeframe proof.")
        decision = next(
            item for item in trace.planner_decisions if item.request_id == planner_request_id
        )
        if decision.selected_timeframe != bundle.timeframe:
            raise ValueError("Planner decision does not authorize the supplied bundle timeframe.")
        record = AdaptiveChildProofRecord.create(
            role=role,
            parent_wave_id=parent.wave_id,
            planner_decision_hash=decision.content_hash,
            data_manifest_hash=manifest.content_hash,
            generation_request_hash=generation_request.content_hash,
            candidate_graph=graph,
            deterministic_result=result,
            generation_depth=parent.generation_depth + 1,
            created_at_utc=created_at_utc,
        )
        trace_with_record = self._replace_trace(
            trace,
            child_proofs=(*trace.child_proofs, record),
            updated_at_utc=created_at_utc,
        )
        decision_summary = adaptive_parent_reconstruction_decision(
            trace_with_record,
            role=role,
            parent_wave_id=parent.wave_id,
        )
        stop_reason = AdaptiveStopReason.NONE
        status = AdaptiveRecountStatus.RECURSIVE_PROOF_ACTIVE
        if decision_summary.search_outcome is AdaptiveSearchOutcome.RECOUNT_REQUIRED:
            stop_reason = AdaptiveStopReason.RECOUNT_REQUIRED
        elif decision_summary.search_outcome is AdaptiveSearchOutcome.CANDIDATE_SEARCH_EXHAUSTED:
            stop_reason = AdaptiveStopReason.CANDIDATE_SEARCH_EXHAUSTED
            status = AdaptiveRecountStatus.UNRESOLVED
        elif decision_summary.search_outcome is AdaptiveSearchOutcome.NOT_COVERED:
            stop_reason = AdaptiveStopReason.LOWER_TIMEFRAME_NOT_COVERED
            status = AdaptiveRecountStatus.NOT_COVERED
        elif (
            decision_summary.search_outcome
            is AdaptiveSearchOutcome.PARENT_STRUCTURALLY_INCONSISTENT
        ):
            stop_reason = AdaptiveStopReason.NO_VALID_CANDIDATE
            status = AdaptiveRecountStatus.INCONSISTENT
        return self._replace_trace(
            trace_with_record,
            status=status,
            stop_reason=stop_reason,
            updated_at_utc=created_at_utc,
        )

    @staticmethod
    def _lineage_pivot(source: TypedPivot, catalog: Sequence[TypedPivot]) -> TypedPivot | None:
        exact = [
            item
            for item in catalog
            if math.isclose(
                item.price,
                source.price,
                rel_tol=0.0,
                abs_tol=_PIVOT_LINEAGE_ABSOLUTE_TOLERANCE,
            )
            and item.pivot_type is source.pivot_type
        ]
        if not exact:
            return None
        source_time = _utc(source.timestamp_utc)
        return min(exact, key=lambda item: abs((_utc(item.timestamp_utc) - source_time).total_seconds()))

    @staticmethod
    def _find_parent(
        trace: AdaptiveRecountTrace,
        *,
        role: CandidateGraphRole,
        wave_id: str,
    ) -> _AdaptiveParentView:
        if trace.root_pair is None:
            raise ValueError("Initial candidate pair is missing.")
        hypothesis = (
            trace.root_pair.primary
            if role is CandidateGraphRole.PRIMARY
            else trace.root_pair.alternative
        )
        if hypothesis is None:
            raise ValueError("Selected hypothesis is unavailable.")
        root_matches = [item for item in hypothesis.nodes if item.wave_id == wave_id]
        child_matches = [
            child
            for record in trace.child_proofs
            if record.role is role
            for child in record.candidate_graph.children
            if child.child_id == wave_id
        ]
        if len(root_matches) + len(child_matches) != 1:
            raise ValueError("Parent wave ID must identify exactly one candidate node in its hypothesis.")
        if root_matches:
            item = root_matches[0]
            return _AdaptiveParentView(
                wave_id=item.wave_id,
                degree=item.degree,
                timeframe=item.timeframe,
                direction=item.direction,
                declared_family=item.declared_family,
                completion_state=item.completion_state,
                start_pivot=item.start_pivot,
                end_pivot=item.end_pivot,
                invalidation=item.invalidation,
                diagonal_declaration=item.diagonal_declaration,
                generation_depth=0,
                timeframe_use_purpose=TimeframeUsePurpose.ROOT_STRUCTURE_OBSERVATION,
                structural_parent_degree=None,
                parent_evidence_timeframe=None,
            )
        child = child_matches[0]
        source_record = next(
            record
            for record in trace.child_proofs
            if record.role is role and child in record.candidate_graph.children
        )
        structural_parent = AdaptiveRecountCoordinator._find_parent(
            trace,
            role=role,
            wave_id=source_record.parent_wave_id,
        )
        return _AdaptiveParentView(
            wave_id=child.child_id,
            degree=child.degree,
            timeframe=child.timeframe,
            direction=child.direction,
            declared_family=child.declared_family,
            completion_state=child.completion_state,
            start_pivot=child.start_pivot,
            end_pivot=child.end_pivot,
            invalidation=child.invalidation,
            diagonal_declaration=child.diagonal_declaration,
            generation_depth=source_record.generation_depth,
            timeframe_use_purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
            structural_parent_degree=structural_parent.degree,
            parent_evidence_timeframe=structural_parent.timeframe,
        )

    @classmethod
    def pending_proof_targets(
        cls,
        trace: AdaptiveRecountTrace,
        *,
        role: CandidateGraphRole,
    ) -> tuple[dict[str, Any], ...]:
        """Return unexamined nodes that still require proof or a bounded stop."""

        role = CandidateGraphRole(role)
        if trace.root_pair is None:
            return ()
        hypothesis = trace.root_pair.primary if role is CandidateGraphRole.PRIMARY else trace.root_pair.alternative
        if hypothesis is None:
            return ()
        candidates: list[_AdaptiveParentView] = [
            _AdaptiveParentView(
                wave_id=item.wave_id,
                degree=item.degree,
                timeframe=item.timeframe,
                direction=item.direction,
                declared_family=item.declared_family,
                completion_state=item.completion_state,
                start_pivot=item.start_pivot,
                end_pivot=item.end_pivot,
                invalidation=item.invalidation,
                diagonal_declaration=item.diagonal_declaration,
                generation_depth=0,
                timeframe_use_purpose=TimeframeUsePurpose.ROOT_STRUCTURE_OBSERVATION,
                structural_parent_degree=None,
                parent_evidence_timeframe=None,
            )
            for item in hypothesis.nodes
        ]
        for record in trace.child_proofs:
            if (
                record.role is not role
                or record.deterministic_result.status
                is not SubdivisionVerificationStatus.UNPROVEN
                or _attempt_disposition(record)
                is not AdaptiveChildAttemptDisposition.UNPROVEN
            ):
                continue
            structural_parent = cls._find_parent(
                trace,
                role=role,
                wave_id=record.parent_wave_id,
            )
            candidates.extend(
                _AdaptiveParentView(
                    wave_id=child.child_id,
                    degree=child.degree,
                    timeframe=child.timeframe,
                    direction=child.direction,
                    declared_family=child.declared_family,
                    completion_state=child.completion_state,
                    start_pivot=child.start_pivot,
                    end_pivot=child.end_pivot,
                    invalidation=child.invalidation,
                    diagonal_declaration=child.diagonal_declaration,
                    generation_depth=record.generation_depth,
                    timeframe_use_purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
                    structural_parent_degree=structural_parent.degree,
                    parent_evidence_timeframe=structural_parent.timeframe,
                )
                for child in record.candidate_graph.children
            )
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            if item.wave_id in seen or item.completion_state == "projected":
                continue
            seen.add(item.wave_id)
            if item.degree == trace.plan.budgets.terminal_degree:
                continue
            existing = _proof_records_for(
                trace,
                role=role,
                parent_wave_id=item.wave_id,
            )
            rejections = _reconstruction_rejections_for(
                trace,
                role=role,
                parent_wave_id=item.wave_id,
            )
            if existing or rejections:
                reconstruction = adaptive_parent_reconstruction_decision(
                    trace,
                    role=role,
                    parent_wave_id=item.wave_id,
                )
                if reconstruction.search_outcome is not AdaptiveSearchOutcome.RECOUNT_REQUIRED:
                    continue
            request_id = next(
                (
                    request.request_id
                    for request in reversed(trace.planner_requests)
                    if request.parent_candidate_id == f"{role.value}:{item.wave_id}"
                ),
                None,
            )
            decision = (
                next((value for value in trace.planner_decisions if value.request_id == request_id), None)
                if request_id is not None
                else None
            )
            terminal_decision = decision is not None and decision.status in {
                TimeframePlanningStatus.NOT_COVERED,
                TimeframePlanningStatus.UNSUPPORTED,
                TimeframePlanningStatus.BUDGET_EXHAUSTED,
                TimeframePlanningStatus.UNRESOLVED,
            }
            if item.generation_depth >= trace.plan.budgets.maximum_recursion_depth:
                terminal_decision = True
            if terminal_decision:
                continue
            results.append(
                {
                    "role": role.value,
                    "parent_wave_id": item.wave_id,
                    "parent_degree": item.degree,
                    "parent_timeframe": item.timeframe,
                    "completion_state": item.completion_state,
                    "generation_depth": item.generation_depth,
                    "start_pivot_id": item.start_pivot.pivot_id,
                    "end_pivot_id": item.end_pivot.pivot_id,
                }
            )
        return tuple(results)

    @staticmethod
    def mark_ready_for_final_comparison(trace: AdaptiveRecountTrace, *, updated_at_utc: str) -> AdaptiveRecountTrace:
        if trace.root_pair is None:
            raise ValueError("No frozen initial candidates exist.")
        surviving_roles: list[CandidateGraphRole] = []
        candidates = (
            (
                CandidateGraphRole.PRIMARY,
                trace.root_pair.primary,
                trace.root_pair.primary_screen,
            ),
            (
                CandidateGraphRole.ALTERNATIVE,
                trace.root_pair.alternative,
                trace.root_pair.alternative_screen,
            ),
        )
        for role, hypothesis, screen in candidates:
            if (
                hypothesis is None
                or screen is None
                or screen.status is not CandidateScreenStatus.SURVIVING_UNPROVEN
            ):
                continue
            roots = tuple(item for item in hypothesis.nodes if item.parent_wave_id is None)
            if not roots:
                continue
            root_statuses = tuple(
                effective_adaptive_proof_status(
                    trace,
                    role=role,
                    parent_wave_id=root.wave_id,
                )
                for root in roots
            )
            if all(
                status is not SubdivisionVerificationStatus.INCONSISTENT
                for status in root_statuses
            ):
                surviving_roles.append(role)
        if not surviving_roles:
            return AdaptiveRecountCoordinator._replace_trace(
                trace,
                status=AdaptiveRecountStatus.UNRESOLVED,
                stop_reason=AdaptiveStopReason.NO_VALID_CANDIDATE,
                updated_at_utc=updated_at_utc,
            )
        pending = tuple(
            target
            for role in surviving_roles
            for target in AdaptiveRecountCoordinator.pending_proof_targets(trace, role=role)
        )
        if pending:
            return AdaptiveRecountCoordinator._replace_trace(
                trace,
                status=AdaptiveRecountStatus.UNRESOLVED,
                stop_reason=AdaptiveStopReason.PENDING_STRUCTURAL_EVIDENCE,
                warnings=(
                    *trace.warnings,
                    "Pre-freeze proof targets remain: "
                    + ", ".join(f"{item['role']}:{item['parent_wave_id']}" for item in pending),
                ),
                updated_at_utc=updated_at_utc,
            )
        return AdaptiveRecountCoordinator._replace_trace(
            trace,
            status=AdaptiveRecountStatus.READY_FOR_FINAL_COMPARISON,
            stop_reason=AdaptiveStopReason.READY_FOR_FINAL_COMPARISON,
            updated_at_utc=updated_at_utc,
        )

    @staticmethod
    def mark_authoritative_freeze(
        trace: AdaptiveRecountTrace,
        *,
        source_analysis_run_id: int,
        degree_resolution_id: int,
        updated_at_utc: str,
    ) -> AdaptiveRecountTrace:
        if trace.status is not AdaptiveRecountStatus.READY_FOR_FINAL_COMPARISON:
            raise ValueError("Adaptive trace must complete pre-freeze comparison first.")
        return AdaptiveRecountCoordinator._replace_trace(
            trace,
            source_analysis_run_id=source_analysis_run_id,
            authoritative_degree_resolution_id=degree_resolution_id,
            status=AdaptiveRecountStatus.FROZEN_BY_RESOLVE_DEGREES,
            stop_reason=AdaptiveStopReason.NONE,
            updated_at_utc=updated_at_utc,
        )

    @staticmethod
    def _replace_trace(trace: AdaptiveRecountTrace, **changes: Any) -> AdaptiveRecountTrace:
        values = trace.to_dict()
        values.update({key: _json_value(value) for key, value in changes.items()})
        # Rebuild typed fields directly to retain immutable contracts.
        typed = {
            "trace_id": changes.get("trace_id", trace.trace_id),
            "plan": changes.get("plan", trace.plan),
            "status": changes.get("status", trace.status),
            "stop_reason": changes.get("stop_reason", trace.stop_reason),
            "planner_requests": changes.get("planner_requests", trace.planner_requests),
            "planner_decisions": changes.get("planner_decisions", trace.planner_decisions),
            "data_manifests": changes.get("data_manifests", trace.data_manifests),
            "bundle_hashes": changes.get("bundle_hashes", trace.bundle_hashes),
            "bundle_validations": changes.get("bundle_validations", trace.bundle_validations),
            "root_pair": changes.get("root_pair", trace.root_pair),
            "child_proofs": changes.get("child_proofs", trace.child_proofs),
            "reconstruction_rejections": changes.get(
                "reconstruction_rejections",
                trace.reconstruction_rejections,
            ),
            "root_stage_failures": changes.get(
                "root_stage_failures",
                trace.root_stage_failures,
            ),
            "root_stage_diagnostics": changes.get(
                "root_stage_diagnostics",
                trace.root_stage_diagnostics,
            ),
            "root_stage_call_count": changes.get(
                "root_stage_call_count",
                trace.root_stage_call_count,
            ),
            "source_analysis_run_id": changes.get("source_analysis_run_id", trace.source_analysis_run_id),
            "authoritative_degree_resolution_id": changes.get("authoritative_degree_resolution_id", trace.authoritative_degree_resolution_id),
            "warnings": changes.get("warnings", trace.warnings),
            "created_at_utc": changes.get("created_at_utc", trace.created_at_utc),
            "updated_at_utc": changes.get("updated_at_utc", trace.updated_at_utc),
            "schema_version": trace.schema_version,
        }
        return AdaptiveRecountTrace.create(**typed)


def load_native_bundle(path: Path) -> NativeOHLCVBundle:
    import json

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("Native bundle file must contain one JSON object.")
    return NativeOHLCVBundle.from_dict(value)


def load_adaptive_recount_trace(path: Path) -> AdaptiveRecountTrace:
    import json

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("Adaptive recount trace file must contain one JSON object.")
    return AdaptiveRecountTrace.from_dict(value)


def write_immutable_adaptive_trace(
    trace: AdaptiveRecountTrace,
    output_directory: Path,
) -> Path:
    """Atomically write one content-addressed trace without overwriting history."""

    if trace.content_hash != adaptive_recount_trace_content_hash(trace):
        raise ValueError("Adaptive trace hash is invalid.")
    directory = Path(output_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{trace.trace_id}_{trace.content_hash[:20]}.json"
    if target.exists():
        raise FileExistsError(f"Immutable adaptive trace already exists: {target}")
    payload = (canonical_forecast_json(trace.to_dict()) + "\n").encode("utf-8")
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists():
        raise FileExistsError(f"Temporary adaptive trace already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise FileExistsError(f"Immutable adaptive trace already exists: {target}")
        temporary.rename(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


__all__ = [
    "ADAPTIVE_RECOUNT_POLICY_VERSION",
    "ADAPTIVE_RECOUNT_SCHEMA_VERSION",
    "ADAPTIVE_RECONSTRUCTION_REJECTION_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_CANDIDATE_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_CLASSIFICATION_OUTPUT_SCHEMA",
    "ADAPTIVE_ROOT_GROUPING_OUTPUT_SCHEMA",
    "ADAPTIVE_ROOT_GROUP_CLASSIFICATION_OUTPUT_SCHEMA",
    "ADAPTIVE_ROOT_OUTPUT_SCHEMA",
    "ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA",
    "ADAPTIVE_ROOT_SEGMENTATION_OUTPUT_SCHEMA_V3",
    "ADAPTIVE_ROOT_STAGE_DIAGNOSTIC_REFERENCE_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_STAGE_DIAGNOSTIC_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_STAGE_FAILURE_SCHEMA_VERSION",
    "ADAPTIVE_ROOT_STAGE_REPAIR_CONTEXT_SCHEMA_VERSION",
    "ADAPTIVE_TRACE_SCHEMA_VERSION",
    "MAX_ROOT_STAGE_ATTEMPTS",
    "ROOT_STAGE_REPAIR_INSTRUCTION",
    "RKLB_STRICT_BLIND_POLICY_ID",
    "AdaptiveCandidateScreen",
    "AdaptiveChildProofRecord",
    "AdaptiveChildReconstructionRejection",
    "AdaptiveRecountBudget",
    "AdaptiveRecountCoordinator",
    "AdaptiveRecountPlan",
    "AdaptiveRecountStatus",
    "AdaptiveRecountTrace",
    "AdaptiveRootCandidateProvider",
    "AdaptiveGroupedRootCandidateProvider",
    "AdaptiveStagedRootCandidateProvider",
    "AdaptiveRootHypothesis",
    "AdaptiveRootPair",
    "AdaptiveStopReason",
    "AdaptiveWaveCandidate",
    "CandidateScreenStatus",
    "RejectedRootStageDiagnostic",
    "RootGenerationStage",
    "RootStageBoundaryOrdinal",
    "RootStageDiagnosticReference",
    "RootStageFailureCode",
    "RootStageParseError",
    "RootStageRepairContext",
    "RootStageRepairFact",
    "RootStageValidationFailure",
    "adaptive_candidate_screen_content_hash",
    "adaptive_child_policies",
    "adaptive_child_proof_record_content_hash",
    "adaptive_hypothesis_signature",
    "adaptive_parent_reconstruction_decision",
    "adaptive_parent_structural_assessment",
    "adaptive_reconstruction_rejection_content_hash",
    "adaptive_recount_plan_content_hash",
    "adaptive_recount_trace_content_hash",
    "effective_adaptive_proof_status",
    "adaptive_root_hypothesis_content_hash",
    "adaptive_root_pair_content_hash",
    "rejected_root_stage_diagnostic_content_hash",
    "root_stage_diagnostic_reference_content_hash",
    "root_stage_failure_content_hash",
    "root_stage_repair_context_content_hash",
    "root_stage_semantic_signature",
    "adaptive_root_classification_schema_for_packet",
    "adaptive_root_group_classification_schema_for_packet",
    "allowed_root_degrees_for_timeframes",
    "build_adaptive_analysis_scope",
    "build_blind_root_packet",
    "build_candidate_scoped_evidence",
    "build_root_classification_packet",
    "build_root_group_classification_packet",
    "build_root_grouping_packet",
    "build_root_segmentation_packet",
    "create_adaptive_recount_plan",
    "load_native_bundle",
    "load_adaptive_recount_trace",
    "native_window_from_bundle",
    "parse_root_segmentation_output",
    "parse_root_grouping_output",
    "rklb_strict_blind_plan",
    "screen_adaptive_hypothesis",
    "write_immutable_adaptive_trace",
    "write_immutable_root_stage_diagnostic",
]
