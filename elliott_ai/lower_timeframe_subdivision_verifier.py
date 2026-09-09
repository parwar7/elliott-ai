"""Pure, shadow-only lower-timeframe candidate subdivision verification.

This module accepts frozen candidate graphs and caller-supplied native OHLCV
windows.  It has no provider, persistence, market-data fetch, CLI, forecast,
or active-pipeline integration.  The four status values defined here are the
only structural-status authority for this shadow verifier.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, time, timezone
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .adaptive_structure import (
    BoundaryValidityStatus,
    DiagonalDeclaration,
    calculate_diagonal_geometry,
    calculate_motive_hard_rules,
    evaluate_candidate_boundary,
    validate_direct_child_degree,
)
from .correction_semantics import MOTIVE_FAMILIES
from .forecast_records import canonical_sha256
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGenerationPair,
    CandidateGenerationPolicy,
    CandidateInvalidation,
    CandidateProofScope,
    CoverageLimitation,
    CoverageLimitationCode,
    GeneratedChildWave,
    InvalidationDirection,
    InvalidationEvaluationBasis,
    LowerTimeframeCandidateGenerationRequest,
    NativeCandle,
    NativeOHLCVWindow,
    ParentCandidateSnapshot,
    TypedPivot,
    WindowCoverageRole,
    candidate_child_graph_content_hash,
    candidate_generation_request_content_hash,
    expected_child_positions,
    family_children_are_admissible,
    native_window_covers_parent,
    native_window_content_hash,
    native_window_rows_hash,
    parent_candidate_content_hash,
)
from .market_data_identity import dataset_feed_family_matches
from .pivot_chronology import (
    child_graph_boundary_path,
    chronology_groups_for_typed_pivots,
    first_chronology_conflict,
)
from .schema import STANDARD_DEGREES
from .timeframes import normalize_timeframe_name


LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_SCHEMA_VERSION = (
    "lower-timeframe-subdivision-verifier-1.0.0"
)
LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_POLICY_VERSION = (
    "lower-timeframe-subdivision-verifier-policy-1.0.0"
)
LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_CALCULATION_VERSION = (
    "lower-timeframe-subdivision-verifier-calculation-1.2.0"
)
SUBDIVISION_VERIFIER_RESULT_SCHEMA_VERSION = (
    "lower-timeframe-subdivision-verifier-result-1.2.0"
)
SUBDIVISION_VERIFIER_PAIR_SCHEMA_VERSION = (
    "lower-timeframe-subdivision-verifier-pair-1.0.0"
)

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_EPSILON = 1e-9


class SubdivisionVerificationStatus(StrEnum):
    """The verifier's complete and exclusive structural-status vocabulary."""

    VERIFIED = "verified"
    UNPROVEN = "unproven"
    NOT_COVERED = "not_covered"
    INCONSISTENT = "inconsistent"


class VerificationReasonCode(StrEnum):
    SHADOW_MODE_REQUIRED = "shadow_mode_required"
    SOURCE_INTEGRITY_FAILED = "source_integrity_failed"
    REQUEST_HASH_MISMATCH = "request_hash_mismatch"
    GRAPH_HASH_MISMATCH = "graph_hash_mismatch"
    GRAPH_REQUEST_MISMATCH = "graph_request_mismatch"
    CANDIDATE_ONLY_REQUIRED = "candidate_only_required"
    PARENT_NOT_COMPLETED = "parent_not_completed"
    RECURSION_DEPTH_EXHAUSTED = "recursion_depth_exhausted"
    TERMINAL_DEGREE_NOT_REACHED = "terminal_degree_not_reached"
    REQUIRED_WINDOW_MISSING = "required_window_missing"
    REQUIRED_WINDOW_NOT_NATIVE = "required_window_not_native"
    WINDOW_METADATA_INCOMPATIBLE = "window_metadata_incompatible"
    WINDOW_AFTER_CUTOFF = "window_after_cutoff"
    INCOMPLETE_CANDLE = "incomplete_candle"
    FULL_COVERAGE_MISSING = "full_coverage_missing"
    MINIMUM_NATIVE_BARS_NOT_MET = "minimum_native_bars_not_met"
    PIVOT_NOT_CATALOGUED = "pivot_not_catalogued"
    PIVOT_LINEAGE_MISMATCH = "pivot_lineage_mismatch"
    PIVOT_VALUE_MISMATCH = "pivot_value_mismatch"
    INTRABAR_PIVOT_ORDER_UNRESOLVED = "intrabar_pivot_order_unresolved"
    PARENT_CHILD_BOUNDARY_MISMATCH = "parent_child_boundary_mismatch"
    CHILD_PIVOT_CHAIN_DISCONTINUOUS = "child_pivot_chain_discontinuous"
    CHILD_ORDER_INVALID = "child_order_invalid"
    FAMILY_CHILDREN_INCOMPATIBLE = "family_children_incompatible"
    DIRECTION_SEQUENCE_INVALID = "direction_sequence_invalid"
    SAME_DEGREE_PARENT_CHILD_FORBIDDEN = "same_degree_parent_child_forbidden"
    HARD_PRICE_RULE_FAILURE = "hard_price_rule_failure"
    BOUNDARY_REQUIRES_RESELECTION = "boundary_requires_reselection"
    PIVOT_NOT_TRUE_EXTREME = "pivot_not_true_extreme"
    CANDIDATE_SEGMENTATION_FAILED = "candidate_segmentation_failed"
    DIAGONAL_DECLARATION_REQUIRED = "diagonal_declaration_required"
    DIAGONAL_GEOMETRY_FAILURE = "diagonal_geometry_failure"
    DIAGONAL_CHILD_FAMILY_INVALID = "diagonal_child_family_invalid"
    DIAGONAL_GEOMETRY_UNPROVEN = "diagonal_geometry_unproven"
    INVALIDATION_PIVOT_UNKNOWN = "invalidation_pivot_unknown"
    INVALIDATION_BREACHED = "invalidation_breached"
    TERMINAL_LEAF_VERIFIED = "terminal_leaf_verified"
    RAW_MODEL_PROSE_UNTRUSTED = "raw_model_prose_untrusted"


class NextEvidenceCode(StrEnum):
    PROVIDE_FULL_REQUIRED_WINDOW = "provide_full_required_window"
    PROVIDE_NATIVE_ROWS = "provide_native_rows"
    PROVIDE_COMPATIBLE_METADATA = "provide_compatible_metadata"
    PROVIDE_TYPED_PIVOT_LINEAGE = "provide_typed_pivot_lineage"
    PROVIDE_TERMINAL_CHILD_GRAPH = "provide_terminal_child_graph"
    PROVIDE_DIAGONAL_GEOMETRY = "provide_diagonal_geometry"
    RESELECT_CANDIDATE_BOUNDARIES = "reselect_candidate_boundaries"
    RECONSTRUCT_CHILD_CANDIDATE = "reconstruct_child_candidate"
    CORRECT_HARD_PRICE_RULE = "correct_hard_price_rule"
    CORRECT_INVALIDATION = "correct_invalidation"
    LOWER_TIMEFRAME_RESOLUTION_REQUIRED = "lower_timeframe_resolution_required"


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


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


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


def _enum(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {choices}.") from exc


def _string_tuple(value: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(_require_text(item, field_name=field_name) for item in value)
    return result


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, model_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


def _model(value: Any, model_type: type[Any], *, field_name: str) -> Any:
    if isinstance(value, model_type):
        return value
    from_dict = getattr(model_type, "from_dict", None)
    if isinstance(value, Mapping) and callable(from_dict):
        return from_dict(value)
    raise TypeError(f"{field_name} must be a {model_type.__name__}.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SubdivisionCheck(_JsonContract):
    check_id: str
    scope: str
    required: bool
    applicable: bool
    passed: bool | None
    reason_code: VerificationReasonCode
    evidence_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _require_text(self.check_id, field_name="check_id"))
        object.__setattr__(self, "scope", _require_text(self.scope, field_name="scope"))
        if not isinstance(self.required, bool) or not isinstance(self.applicable, bool):
            raise TypeError("SubdivisionCheck required and applicable must be booleans.")
        if self.passed is not None and not isinstance(self.passed, bool):
            raise TypeError("SubdivisionCheck passed must be boolean or null.")
        object.__setattr__(self, "reason_code", _enum(self.reason_code, VerificationReasonCode, field_name="reason_code"))
        object.__setattr__(self, "evidence_ids", _string_tuple(self.evidence_ids, field_name="evidence_ids"))
        if not isinstance(self.details, Mapping):
            raise TypeError("SubdivisionCheck details must be a mapping.")
        object.__setattr__(self, "details", _freeze_json(self.details))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubdivisionCheck":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class VerificationProofScope(_JsonContract):
    target_timeframe: str
    verified_to_timeframe: str | None
    terminal_degree: str
    recursion_depth: int
    coverage_limitations: tuple[CoverageLimitation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_timeframe", normalize_timeframe_name(_require_text(self.target_timeframe, field_name="target_timeframe")))
        if self.verified_to_timeframe is not None:
            text = normalize_timeframe_name(_require_text(self.verified_to_timeframe, field_name="verified_to_timeframe"))
            object.__setattr__(self, "verified_to_timeframe", text)
        if self.terminal_degree not in STANDARD_DEGREES or self.terminal_degree == "Unassigned":
            raise ValueError("terminal_degree must be a standard assigned degree.")
        if not isinstance(self.recursion_depth, int) or isinstance(self.recursion_depth, bool) or self.recursion_depth < 0:
            raise ValueError("recursion_depth must be a non-negative integer.")
        if isinstance(self.coverage_limitations, (str, bytes)) or not isinstance(self.coverage_limitations, Sequence):
            raise TypeError("coverage_limitations must be a sequence.")
        limitations = tuple(_model(item, CoverageLimitation, field_name="coverage_limitations") for item in self.coverage_limitations)
        object.__setattr__(self, "coverage_limitations", limitations)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerificationProofScope":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class SubdivisionVerifierPolicy(_JsonContract):
    policy_id: str
    maximum_recursion_depth: int
    terminal_degree: str
    timeframe_by_child_degree: Mapping[str, str]
    minimum_native_bars: int = 6
    schema_version: str = LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _require_text(self.policy_id, field_name="policy_id"))
        if not isinstance(self.maximum_recursion_depth, int) or isinstance(self.maximum_recursion_depth, bool) or not 1 <= self.maximum_recursion_depth <= 8:
            raise ValueError("maximum_recursion_depth must be between 1 and 8.")
        if self.terminal_degree not in STANDARD_DEGREES or self.terminal_degree == "Unassigned":
            raise ValueError("terminal_degree must be a standard assigned degree.")
        if not isinstance(self.timeframe_by_child_degree, Mapping):
            raise TypeError("timeframe_by_child_degree must be a mapping.")
        mapping: dict[str, str] = {}
        for degree, timeframe in self.timeframe_by_child_degree.items():
            degree_name = _require_text(degree, field_name="timeframe_by_child_degree.degree")
            timeframe_name = normalize_timeframe_name(_require_text(timeframe, field_name="timeframe_by_child_degree.timeframe"))
            if degree_name not in STANDARD_DEGREES or degree_name == "Unassigned":
                raise ValueError("timeframe_by_child_degree has an invalid degree.")
            mapping[degree_name] = timeframe_name
        if not mapping:
            raise ValueError("timeframe_by_child_degree cannot be empty.")
        object.__setattr__(self, "timeframe_by_child_degree", MappingProxyType(dict(sorted(mapping.items()))))
        if not isinstance(self.minimum_native_bars, int) or self.minimum_native_bars < 2:
            raise ValueError("minimum_native_bars must be at least two.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != subdivision_verifier_policy_content_hash(self):
            raise ValueError("SubdivisionVerifierPolicy content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "SubdivisionVerifierPolicy":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=subdivision_verifier_policy_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "SubdivisionVerifierPolicy":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != subdivision_verifier_policy_content_hash(result):
            raise ValueError("SubdivisionVerifierPolicy content_hash does not match its payload.")
        return result


def subdivision_verifier_policy_content_hash(value: SubdivisionVerifierPolicy | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, SubdivisionVerifierPolicy) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def googl_intermediate_to_minute_verifier_policy() -> SubdivisionVerifierPolicy:
    """Return the approved first GOOGL proof policy without running it."""
    return SubdivisionVerifierPolicy.create(
        policy_id="googl-lower-timeframe-subdivision-verifier-policy-1.0.0",
        maximum_recursion_depth=2,
        terminal_degree="Minute",
        timeframe_by_child_degree={"Minor": "daily", "Minute": "4h"},
        minimum_native_bars=6,
    )


@dataclass(frozen=True, slots=True)
class LowerTimeframeSubdivisionVerificationRequest(_JsonContract):
    request_id: str
    generation_request: LowerTimeframeCandidateGenerationRequest
    candidate_graph: CandidateChildGraph
    verifier_policy: SubdivisionVerifierPolicy
    shadow_mode: bool
    created_at_utc: str
    schema_version: str = LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_SCHEMA_VERSION
    calculation_version: str = LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_CALCULATION_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        if not isinstance(self.generation_request, LowerTimeframeCandidateGenerationRequest):
            raise TypeError("generation_request must be a LowerTimeframeCandidateGenerationRequest.")
        if not isinstance(self.candidate_graph, CandidateChildGraph):
            raise TypeError("candidate_graph must be a CandidateChildGraph.")
        if not isinstance(self.verifier_policy, SubdivisionVerifierPolicy):
            raise TypeError("verifier_policy must be a SubdivisionVerifierPolicy.")
        if not isinstance(self.shadow_mode, bool):
            raise TypeError("shadow_mode must be boolean.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        object.__setattr__(self, "calculation_version", _require_text(self.calculation_version, field_name="calculation_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != subdivision_verification_request_content_hash(self):
            raise ValueError("LowerTimeframeSubdivisionVerificationRequest content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "LowerTimeframeSubdivisionVerificationRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("request_id"):
            graph = values.get("candidate_graph")
            graph_hash = graph.content_hash if isinstance(graph, CandidateChildGraph) else ""
            values["request_id"] = f"lower_tf_verify_{canonical_sha256({'graph': graph_hash, 'created_at_utc': values.get('created_at_utc')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=subdivision_verification_request_content_hash(result))


def subdivision_verification_request_content_hash(value: LowerTimeframeSubdivisionVerificationRequest | Mapping[str, Any]) -> str:
    if isinstance(value, LowerTimeframeSubdivisionVerificationRequest):
        payload = value.to_dict()
    else:
        payload = dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def build_subdivision_verification_request(
    generation_request: LowerTimeframeCandidateGenerationRequest,
    candidate_graph: CandidateChildGraph,
    *,
    verifier_policy: SubdivisionVerifierPolicy,
    shadow_mode: bool,
    created_at_utc: str,
    request_id: str | None = None,
) -> LowerTimeframeSubdivisionVerificationRequest:
    """Freeze one graph handoff without loading any data or persistence record."""
    return LowerTimeframeSubdivisionVerificationRequest.create(
        request_id=request_id or "",
        generation_request=generation_request,
        candidate_graph=candidate_graph,
        verifier_policy=verifier_policy,
        shadow_mode=shadow_mode,
        created_at_utc=created_at_utc,
    )


@dataclass(frozen=True, slots=True)
class ChildSubdivisionVerification(_JsonContract):
    child_id: str
    status: SubdivisionVerificationStatus
    reason_codes: tuple[VerificationReasonCode, ...]
    check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "child_id", _require_text(self.child_id, field_name="child_id"))
        object.__setattr__(self, "status", _enum(self.status, SubdivisionVerificationStatus, field_name="status"))
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        object.__setattr__(self, "reason_codes", tuple(_enum(item, VerificationReasonCode, field_name="reason_codes") for item in self.reason_codes))
        object.__setattr__(self, "check_ids", _string_tuple(self.check_ids, field_name="check_ids"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChildSubdivisionVerification":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class SubdivisionVerifierResult(_JsonContract):
    result_id: str
    verification_request_id: str
    verification_request_hash: str
    graph_id: str
    graph_content_hash: str
    status: SubdivisionVerificationStatus
    proof_scope: VerificationProofScope
    child_results: tuple[ChildSubdivisionVerification, ...]
    checks: tuple[SubdivisionCheck, ...]
    reason_codes: tuple[VerificationReasonCode, ...]
    required_next_evidence: tuple[NextEvidenceCode, ...]
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = SUBDIVISION_VERIFIER_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _require_text(self.result_id, field_name="result_id"))
        object.__setattr__(self, "verification_request_id", _require_text(self.verification_request_id, field_name="verification_request_id"))
        object.__setattr__(self, "verification_request_hash", _require_hash(self.verification_request_hash, field_name="verification_request_hash"))
        object.__setattr__(self, "graph_id", _require_text(self.graph_id, field_name="graph_id"))
        object.__setattr__(self, "graph_content_hash", _require_hash(self.graph_content_hash, field_name="graph_content_hash"))
        object.__setattr__(self, "status", _enum(self.status, SubdivisionVerificationStatus, field_name="status"))
        object.__setattr__(self, "proof_scope", _model(self.proof_scope, VerificationProofScope, field_name="proof_scope"))
        if isinstance(self.child_results, (str, bytes)) or not isinstance(self.child_results, Sequence):
            raise TypeError("child_results must be a sequence.")
        object.__setattr__(self, "child_results", tuple(_model(item, ChildSubdivisionVerification, field_name="child_results") for item in self.child_results))
        if isinstance(self.checks, (str, bytes)) or not isinstance(self.checks, Sequence):
            raise TypeError("checks must be a sequence.")
        object.__setattr__(self, "checks", tuple(_model(item, SubdivisionCheck, field_name="checks") for item in self.checks))
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        object.__setattr__(self, "reason_codes", tuple(_enum(item, VerificationReasonCode, field_name="reason_codes") for item in self.reason_codes))
        if isinstance(self.required_next_evidence, (str, bytes)) or not isinstance(self.required_next_evidence, Sequence):
            raise TypeError("required_next_evidence must be a sequence.")
        object.__setattr__(self, "required_next_evidence", tuple(_enum(item, NextEvidenceCode, field_name="required_next_evidence") for item in self.required_next_evidence))
        object.__setattr__(self, "warnings", _string_tuple(self.warnings, field_name="warnings"))
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != subdivision_verifier_result_content_hash(self):
            raise ValueError("SubdivisionVerifierResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "SubdivisionVerifierResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("result_id"):
            seed = {
                "verification_request_hash": values.get("verification_request_hash"),
                "graph_content_hash": values.get("graph_content_hash"),
                "status": _json_value(values.get("status")),
            }
            values["result_id"] = f"lower_tf_result_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=subdivision_verifier_result_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "SubdivisionVerifierResult":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != subdivision_verifier_result_content_hash(result):
            raise ValueError("SubdivisionVerifierResult content_hash does not match its payload.")
        return result


def subdivision_verifier_result_content_hash(value: SubdivisionVerifierResult | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, SubdivisionVerifierResult) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class SubdivisionVerificationPair(_JsonContract):
    pair_id: str
    primary_result: SubdivisionVerifierResult
    alternative_result: SubdivisionVerifierResult
    created_at_utc: str
    schema_version: str = SUBDIVISION_VERIFIER_PAIR_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _require_text(self.pair_id, field_name="pair_id"))
        object.__setattr__(self, "primary_result", _model(self.primary_result, SubdivisionVerifierResult, field_name="primary_result"))
        object.__setattr__(self, "alternative_result", _model(self.alternative_result, SubdivisionVerifierResult, field_name="alternative_result"))
        if self.primary_result.graph_id == self.alternative_result.graph_id:
            raise ValueError("Primary and alternative results must refer to distinct graphs.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != subdivision_verification_pair_content_hash(self):
            raise ValueError("SubdivisionVerificationPair content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "SubdivisionVerificationPair":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("pair_id"):
            seed = {
                "primary": _json_value(values.get("primary_result")),
                "alternative": _json_value(values.get("alternative_result")),
            }
            values["pair_id"] = f"lower_tf_verify_pair_{canonical_sha256(seed)[:32]}"
        result = cls(**values)
        return replace(result, content_hash=subdivision_verification_pair_content_hash(result))


def subdivision_verification_pair_content_hash(value: SubdivisionVerificationPair | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, SubdivisionVerificationPair) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _inclusive_parent_end(parent: ParentCandidateSnapshot) -> datetime:
    end = _utc(parent.end_pivot.timestamp_utc)
    if parent.end_is_date_only:
        return datetime.combine(end.date(), time.max, tzinfo=timezone.utc)
    return end


def _check(
    checks: list[SubdivisionCheck],
    *,
    scope: str,
    required: bool,
    applicable: bool,
    passed: bool | None,
    reason: VerificationReasonCode,
    evidence_ids: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
) -> None:
    checks.append(
        SubdivisionCheck(
            check_id=f"check_{len(checks) + 1:03d}",
            scope=scope,
            required=required,
            applicable=applicable,
            passed=passed,
            reason_code=reason,
            evidence_ids=tuple(evidence_ids),
            details=details or {},
        )
    )


def _result_status(checks: Sequence[SubdivisionCheck], *, terminal_reached: bool) -> SubdivisionVerificationStatus:
    unproven_failures = {
        VerificationReasonCode.RECURSION_DEPTH_EXHAUSTED,
        VerificationReasonCode.TERMINAL_DEGREE_NOT_REACHED,
        VerificationReasonCode.MINIMUM_NATIVE_BARS_NOT_MET,
        VerificationReasonCode.PARENT_NOT_COMPLETED,
        VerificationReasonCode.BOUNDARY_REQUIRES_RESELECTION,
        VerificationReasonCode.PIVOT_NOT_TRUE_EXTREME,
        VerificationReasonCode.CANDIDATE_SEGMENTATION_FAILED,
        VerificationReasonCode.DIAGONAL_DECLARATION_REQUIRED,
        VerificationReasonCode.DIAGONAL_GEOMETRY_UNPROVEN,
        VerificationReasonCode.INTRABAR_PIVOT_ORDER_UNRESOLVED,
    }
    if any(
        check.required
        and check.passed is False
        and check.reason_code not in {VerificationReasonCode.FULL_COVERAGE_MISSING, *unproven_failures}
        for check in checks
    ):
        return SubdivisionVerificationStatus.INCONSISTENT
    if any(
        check.required
        and check.reason_code is VerificationReasonCode.FULL_COVERAGE_MISSING
        and check.passed is False
        for check in checks
    ):
        return SubdivisionVerificationStatus.NOT_COVERED
    if terminal_reached and all(not check.required or check.passed is True for check in checks):
        return SubdivisionVerificationStatus.VERIFIED
    return SubdivisionVerificationStatus.UNPROVEN


def _required_next_evidence(reason_codes: Sequence[VerificationReasonCode]) -> tuple[NextEvidenceCode, ...]:
    result: list[NextEvidenceCode] = []
    mapping = {
        VerificationReasonCode.FULL_COVERAGE_MISSING: NextEvidenceCode.PROVIDE_FULL_REQUIRED_WINDOW,
        VerificationReasonCode.REQUIRED_WINDOW_MISSING: NextEvidenceCode.PROVIDE_FULL_REQUIRED_WINDOW,
        VerificationReasonCode.MINIMUM_NATIVE_BARS_NOT_MET: NextEvidenceCode.PROVIDE_FULL_REQUIRED_WINDOW,
        VerificationReasonCode.REQUIRED_WINDOW_NOT_NATIVE: NextEvidenceCode.PROVIDE_NATIVE_ROWS,
        VerificationReasonCode.WINDOW_METADATA_INCOMPATIBLE: NextEvidenceCode.PROVIDE_COMPATIBLE_METADATA,
        VerificationReasonCode.PIVOT_NOT_CATALOGUED: NextEvidenceCode.PROVIDE_TYPED_PIVOT_LINEAGE,
        VerificationReasonCode.PIVOT_LINEAGE_MISMATCH: NextEvidenceCode.PROVIDE_TYPED_PIVOT_LINEAGE,
        VerificationReasonCode.PIVOT_VALUE_MISMATCH: NextEvidenceCode.PROVIDE_TYPED_PIVOT_LINEAGE,
        VerificationReasonCode.TERMINAL_DEGREE_NOT_REACHED: NextEvidenceCode.PROVIDE_TERMINAL_CHILD_GRAPH,
        VerificationReasonCode.DIAGONAL_GEOMETRY_UNPROVEN: NextEvidenceCode.PROVIDE_DIAGONAL_GEOMETRY,
        VerificationReasonCode.DIAGONAL_DECLARATION_REQUIRED: NextEvidenceCode.PROVIDE_DIAGONAL_GEOMETRY,
        VerificationReasonCode.DIAGONAL_GEOMETRY_FAILURE: NextEvidenceCode.RECONSTRUCT_CHILD_CANDIDATE,
        VerificationReasonCode.DIAGONAL_CHILD_FAMILY_INVALID: NextEvidenceCode.RECONSTRUCT_CHILD_CANDIDATE,
        VerificationReasonCode.HARD_PRICE_RULE_FAILURE: NextEvidenceCode.CORRECT_HARD_PRICE_RULE,
        VerificationReasonCode.INVALIDATION_BREACHED: NextEvidenceCode.CORRECT_INVALIDATION,
        VerificationReasonCode.BOUNDARY_REQUIRES_RESELECTION: NextEvidenceCode.RESELECT_CANDIDATE_BOUNDARIES,
        VerificationReasonCode.PIVOT_NOT_TRUE_EXTREME: NextEvidenceCode.RESELECT_CANDIDATE_BOUNDARIES,
        VerificationReasonCode.CANDIDATE_SEGMENTATION_FAILED: NextEvidenceCode.RECONSTRUCT_CHILD_CANDIDATE,
        VerificationReasonCode.INTRABAR_PIVOT_ORDER_UNRESOLVED: NextEvidenceCode.LOWER_TIMEFRAME_RESOLUTION_REQUIRED,
    }
    for code in reason_codes:
        next_item = mapping.get(code)
        if next_item is not None and next_item not in result:
            result.append(next_item)
    return tuple(result)


def _all_graph_pivots(graph: CandidateChildGraph, parent: ParentCandidateSnapshot) -> tuple[TypedPivot, ...]:
    pivots = [parent.start_pivot, parent.end_pivot]
    for child in graph.children:
        pivots.extend((child.start_pivot, child.end_pivot))
    unique: dict[str, TypedPivot] = {}
    for pivot in pivots:
        unique.setdefault(pivot.pivot_id, pivot)
    return tuple(unique.values())


def _pivot_matches_native_row(pivot: TypedPivot, window: NativeOHLCVWindow) -> bool:
    if pivot.timeframe != window.timeframe or pivot.source_window_hash != window.native_rows_hash:
        return False
    row = next((item for item in window.candles if item.source_row_hash == pivot.source_bar_hash), None)
    if row is None or row.timestamp_utc != pivot.timestamp_utc:
        return False
    raw_price = getattr(row, pivot.price_field.value)
    return abs(float(raw_price) - pivot.price) <= _EPSILON


def _invalidation_breached(
    child: GeneratedChildWave,
    window: NativeOHLCVWindow,
) -> bool:
    """Evaluate a supplied candidate boundary without treating it as Elliott law."""
    invalidation = child.invalidation
    if invalidation is None:
        return False
    start = _utc(child.start_pivot.timestamp_utc)
    completed_end = (
        _utc(child.end_pivot.timestamp_utc)
        if child.completion_state == "completed"
        else None
    )
    for candle in window.candles:
        timestamp = _utc(candle.timestamp_utc)
        if timestamp <= start:
            continue
        if completed_end is not None and timestamp > completed_end:
            break
        if invalidation.evaluation_basis is InvalidationEvaluationBasis.CANDLE_CLOSE:
            observed = candle.close
        elif invalidation.direction is InvalidationDirection.BELOW:
            observed = candle.low
        else:
            observed = candle.high
        if invalidation.direction is InvalidationDirection.BELOW and observed <= invalidation.threshold_price:
            return True
        if invalidation.direction is InvalidationDirection.ABOVE and observed >= invalidation.threshold_price:
            return True
    return False


def _motive_hard_price_rules_pass(
    graph: CandidateChildGraph,
    parent: ParentCandidateSnapshot,
) -> bool:
    if graph.declared_family not in MOTIVE_FAMILIES:
        return True
    children = {child.sequence_position: child for child in graph.children}
    if set(children) != {"1", "2", "3", "4", "5"}:
        return False
    calculation = calculate_motive_hard_rules(
        (
            children["1"].start_pivot.price,
            children["1"].end_pivot.price,
            children["2"].end_pivot.price,
            children["3"].end_pivot.price,
            children["4"].end_pivot.price,
            children["5"].end_pivot.price,
        ),
        direction=parent.direction,
        family=graph.declared_family,
    )
    return calculation.all_hard_rules_passed


def _motive_hard_price_rule_details(
    graph: CandidateChildGraph,
    parent: ParentCandidateSnapshot,
) -> Mapping[str, Any]:
    if graph.declared_family not in MOTIVE_FAMILIES:
        return {}
    children = {child.sequence_position: child for child in graph.children}
    if set(children) != {"1", "2", "3", "4", "5"}:
        return {
            "calculation_available": False,
            "reason": "exactly five motive child positions are required",
        }
    return calculate_motive_hard_rules(
        (
            children["1"].start_pivot.price,
            children["1"].end_pivot.price,
            children["2"].end_pivot.price,
            children["3"].end_pivot.price,
            children["4"].end_pivot.price,
            children["5"].end_pivot.price,
        ),
        direction=parent.direction,
        family=graph.declared_family,
    ).to_dict()


def validate_subdivision_verification_request(
    request: LowerTimeframeSubdivisionVerificationRequest,
) -> tuple[VerificationReasonCode, ...]:
    """Validate frozen handoff integrity without assigning structural status."""
    codes: list[VerificationReasonCode] = []
    generation = request.generation_request
    graph = request.candidate_graph
    policy = request.verifier_policy
    if not request.shadow_mode or not generation.shadow_mode:
        codes.append(VerificationReasonCode.SHADOW_MODE_REQUIRED)
    if generation.content_hash != candidate_generation_request_content_hash(generation):
        codes.append(VerificationReasonCode.REQUEST_HASH_MISMATCH)
    if graph.content_hash != candidate_child_graph_content_hash(graph):
        codes.append(VerificationReasonCode.GRAPH_HASH_MISMATCH)
    if request.content_hash and request.content_hash != subdivision_verification_request_content_hash(request):
        codes.append(VerificationReasonCode.SOURCE_INTEGRITY_FAILED)
    if graph.request_id != generation.request_id or graph.request_content_hash != generation.content_hash:
        codes.append(VerificationReasonCode.GRAPH_REQUEST_MISMATCH)
    if graph.parent_candidate_id != generation.parent_candidate.parent_candidate_id:
        codes.append(VerificationReasonCode.GRAPH_REQUEST_MISMATCH)
    if graph.target_child_degree != generation.target_child_degree or graph.target_timeframe != generation.target_timeframe:
        codes.append(VerificationReasonCode.GRAPH_REQUEST_MISMATCH)
    if graph.proof_scope.verified_to_timeframe is not None:
        codes.append(VerificationReasonCode.CANDIDATE_ONLY_REQUIRED)
    if any(child.candidate_state != "candidate" for child in graph.children):
        codes.append(VerificationReasonCode.CANDIDATE_ONLY_REQUIRED)
    expected_timeframe = policy.timeframe_by_child_degree.get(graph.target_child_degree)
    if expected_timeframe != graph.target_timeframe or policy.terminal_degree != graph.proof_scope.terminal_degree:
        codes.append(VerificationReasonCode.GRAPH_REQUEST_MISMATCH)
    if generation.parent_candidate.generation_depth + 1 > policy.maximum_recursion_depth:
        codes.append(VerificationReasonCode.RECURSION_DEPTH_EXHAUSTED)
    degree_errors = validate_direct_child_degree(
        generation.parent_candidate.degree,
        graph.target_child_degree,
    )
    if "same_degree_parent_child_forbidden" in degree_errors:
        codes.append(VerificationReasonCode.SAME_DEGREE_PARENT_CHILD_FORBIDDEN)
    elif degree_errors:
        codes.append(VerificationReasonCode.GRAPH_REQUEST_MISMATCH)
    return tuple(dict.fromkeys(codes))


class LowerTimeframeSubdivisionVerifier:
    """Deterministically verify one frozen graph; no provider can be supplied."""

    def verify(
        self,
        request: LowerTimeframeSubdivisionVerificationRequest,
        *,
        shadow_mode: bool = False,
    ) -> SubdivisionVerifierResult:
        checks: list[SubdivisionCheck] = []
        generation = request.generation_request
        graph = request.candidate_graph
        parent = generation.parent_candidate
        window = generation.native_ohlcv_scope.required_window

        request_codes = validate_subdivision_verification_request(request)
        caller_shadow_ok = shadow_mode is True
        _check(
            checks,
            scope="shadow_mode",
            required=True,
            applicable=True,
            passed=caller_shadow_ok and VerificationReasonCode.SHADOW_MODE_REQUIRED not in request_codes,
            reason=VerificationReasonCode.SHADOW_MODE_REQUIRED,
        )
        for code in request_codes:
            if code is VerificationReasonCode.SHADOW_MODE_REQUIRED:
                continue
            _check(checks, scope="frozen_handoff", required=True, applicable=True, passed=False, reason=code)

        _check(
            checks,
            scope="parent_candidate_hash",
            required=True,
            applicable=True,
            passed=parent.content_hash == parent_candidate_content_hash(parent),
            reason=VerificationReasonCode.SOURCE_INTEGRITY_FAILED,
            evidence_ids=(parent.parent_candidate_id,),
        )
        _check(
            checks,
            scope="parent_completion",
            required=True,
            applicable=True,
            passed=parent.completion_state == "completed",
            reason=VerificationReasonCode.PARENT_NOT_COMPLETED,
            evidence_ids=(parent.parent_candidate_id,),
        )
        _check(
            checks,
            scope="native_window_hash",
            required=True,
            applicable=True,
            passed=(
                window.content_hash == native_window_content_hash(window)
                and window.native_rows_hash == native_window_rows_hash(window.candles)
            ),
            reason=VerificationReasonCode.SOURCE_INTEGRITY_FAILED,
            evidence_ids=(window.window_id,),
        )

        _check(
            checks,
            scope="native_window",
            required=True,
            applicable=True,
            passed=window.is_native,
            reason=VerificationReasonCode.REQUIRED_WINDOW_NOT_NATIVE,
            evidence_ids=(window.window_id,),
        )
        metadata_ok = dataset_feed_family_matches(parent.dataset_cutoff, window.dataset_cutoff)
        _check(
            checks,
            scope="native_window_metadata",
            required=True,
            applicable=True,
            passed=metadata_ok,
            reason=VerificationReasonCode.WINDOW_METADATA_INCOMPATIBLE,
            evidence_ids=(window.window_id, parent.dataset_cutoff.dataset_id),
        )
        cutoff = _utc(generation.analysis_cutoff_utc)
        cutoff_ok = all(_utc(item.timestamp_utc) <= cutoff for item in window.candles)
        _check(
            checks,
            scope="cutoff",
            required=True,
            applicable=True,
            passed=cutoff_ok,
            reason=VerificationReasonCode.WINDOW_AFTER_CUTOFF,
            evidence_ids=(window.window_id,),
        )
        completed_ok = all(item.completed for item in window.candles)
        _check(
            checks,
            scope="completed_candles",
            required=True,
            applicable=True,
            passed=completed_ok,
            reason=VerificationReasonCode.INCOMPLETE_CANDLE,
            evidence_ids=(window.window_id,),
        )
        enough_rows = len(window.candles) >= request.verifier_policy.minimum_native_bars
        _check(
            checks,
            scope="minimum_native_rows",
            required=True,
            applicable=True,
            passed=enough_rows,
            reason=VerificationReasonCode.MINIMUM_NATIVE_BARS_NOT_MET,
            evidence_ids=(window.window_id,),
            details={"bar_count": len(window.candles), "minimum": request.verifier_policy.minimum_native_bars},
        )
        full_coverage = native_window_covers_parent(window, parent, analysis_cutoff_utc=generation.analysis_cutoff_utc)
        _check(
            checks,
            scope="parent_interval_coverage",
            required=True,
            applicable=True,
            passed=full_coverage,
            reason=VerificationReasonCode.FULL_COVERAGE_MISSING,
            evidence_ids=(window.window_id,),
            details={
                "first_timestamp": window.candles[0].timestamp_utc,
                "last_timestamp": window.candles[-1].timestamp_utc,
                "required_start": parent.start_pivot.timestamp_utc,
                "required_end": _inclusive_parent_end(parent).isoformat(timespec="seconds"),
            },
        )

        catalog = {pivot.pivot_id: pivot for pivot in generation.pivot_catalog}
        all_pivots = _all_graph_pivots(graph, parent)
        for pivot in all_pivots:
            catalogued = catalog.get(pivot.pivot_id)
            _check(
                checks,
                scope=f"pivot:{pivot.pivot_id}",
                required=True,
                applicable=True,
                passed=catalogued is not None,
                reason=VerificationReasonCode.PIVOT_NOT_CATALOGUED,
                evidence_ids=(pivot.pivot_id,),
            )
            if catalogued is None:
                continue
            same_catalog_value = catalogued.content_hash == pivot.content_hash
            _check(
                checks,
                scope=f"pivot_catalog:{pivot.pivot_id}",
                required=True,
                applicable=True,
                passed=same_catalog_value,
                reason=VerificationReasonCode.PIVOT_LINEAGE_MISMATCH,
                evidence_ids=(pivot.pivot_id,),
            )
            native_match = _pivot_matches_native_row(pivot, window)
            _check(
                checks,
                scope=f"pivot_native:{pivot.pivot_id}",
                required=True,
                applicable=True,
                passed=native_match,
                reason=VerificationReasonCode.PIVOT_VALUE_MISMATCH,
                evidence_ids=(pivot.pivot_id, window.window_id),
            )

        chronology_groups = chronology_groups_for_typed_pivots(
            generation.pivot_catalog,
            source_bundle_hash=window.dataset_cutoff.dataset_hash,
        )
        chronology_conflict = first_chronology_conflict(
            child_graph_boundary_path(graph.children),
            chronology_groups,
        )
        _check(
            checks,
            scope="child_pivot_chronology",
            required=True,
            applicable=True,
            passed=chronology_conflict is None,
            reason=VerificationReasonCode.INTRABAR_PIVOT_ORDER_UNRESOLVED,
            evidence_ids=(
                chronology_conflict.selected_boundary_ids
                if chronology_conflict is not None
                else ()
            ),
            details=(
                {
                    **chronology_conflict.to_dict(),
                    "required_relation": (
                        "select_at_most_one_boundary_from_chronology_group"
                    ),
                    "required_next_evidence": (
                        "lower_timeframe_resolution_required"
                    ),
                }
                if chronology_conflict is not None
                else {"intrabar_order_conflict": False}
            ),
        )

        positions = tuple(child.sequence_position for child in graph.children)
        expected = expected_child_positions(graph.declared_family, positions)
        order_ok = expected is not None and positions == expected
        _check(
            checks,
            scope="child_positions",
            required=True,
            applicable=True,
            passed=order_ok,
            reason=VerificationReasonCode.CHILD_ORDER_INVALID,
        )
        child_by_position = {child.sequence_position: child for child in graph.children}
        diagonal_type = (
            graph.diagonal_declaration.diagonal_type
            if graph.diagonal_declaration is not None
            else None
        )
        family_ok = expected is not None and family_children_are_admissible(
            graph.declared_family,
            child_by_position,
            diagonal_type=diagonal_type,
        )
        _check(
            checks,
            scope="child_family",
            required=True,
            applicable=True,
            passed=family_ok,
            reason=VerificationReasonCode.FAMILY_CHILDREN_INCOMPATIBLE,
        )

        if graph.children:
            first = graph.children[0]
            last = graph.children[-1]
            _check(
                checks,
                scope="parent_first_boundary",
                required=True,
                applicable=True,
                passed=parent.start_pivot.content_hash == first.start_pivot.content_hash,
                reason=VerificationReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH,
                evidence_ids=(parent.start_pivot.pivot_id, first.start_pivot.pivot_id),
            )
            _check(
                checks,
                scope="parent_last_boundary",
                required=True,
                applicable=True,
                passed=parent.end_pivot.content_hash == last.end_pivot.content_hash,
                reason=VerificationReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH,
                evidence_ids=(parent.end_pivot.pivot_id, last.end_pivot.pivot_id),
            )
        for previous, current in zip(graph.children, graph.children[1:]):
            connected = previous.end_pivot.content_hash == current.start_pivot.content_hash
            chronology_dependent = (
                chronology_conflict is not None
                and previous.end_pivot.pivot_id
                in chronology_conflict.selected_boundary_ids
                and current.start_pivot.pivot_id
                in chronology_conflict.selected_boundary_ids
            )
            _check(
                checks,
                scope=f"child_chain:{previous.child_id}:{current.child_id}",
                required=True,
                applicable=True,
                passed=connected,
                reason=(
                    VerificationReasonCode.INTRABAR_PIVOT_ORDER_UNRESOLVED
                    if chronology_dependent and not connected
                    else VerificationReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS
                ),
                evidence_ids=(previous.end_pivot.pivot_id, current.start_pivot.pivot_id),
            )

        for child in graph.children:
            boundary = evaluate_candidate_boundary(
                candidate_id=child.child_id,
                direction=child.direction,
                start_timestamp_utc=child.start_pivot.timestamp_utc,
                end_timestamp_utc=child.end_pivot.timestamp_utc,
                start_price=child.start_pivot.price,
                end_price=child.end_pivot.price,
                candles=window.candles,
            )
            if boundary.status is BoundaryValidityStatus.VALID:
                reason = VerificationReasonCode.BOUNDARY_REQUIRES_RESELECTION
                passed = True
            elif boundary.status is BoundaryValidityStatus.PIVOT_NOT_TRUE_EXTREME:
                reason = VerificationReasonCode.PIVOT_NOT_TRUE_EXTREME
                passed = False
            elif boundary.status is BoundaryValidityStatus.CANDIDATE_SEGMENTATION_FAILED:
                reason = VerificationReasonCode.CANDIDATE_SEGMENTATION_FAILED
                passed = False
            else:
                reason = VerificationReasonCode.BOUNDARY_REQUIRES_RESELECTION
                passed = False
            _check(
                checks,
                scope=f"candidate_boundary:{child.child_id}",
                required=True,
                applicable=True,
                passed=passed,
                reason=reason,
                evidence_ids=(child.start_pivot.pivot_id, child.end_pivot.pivot_id),
                details=boundary.to_dict(),
            )

        direction_applicable = graph.declared_family in MOTIVE_FAMILIES
        if direction_applicable:
            expected_directions = ("up", "down", "up", "down", "up") if parent.direction == "up" else ("down", "up", "down", "up", "down")
            _check(
                checks,
                scope="motive_directions",
                required=True,
                applicable=True,
                passed=tuple(child.direction for child in graph.children) == expected_directions,
                reason=VerificationReasonCode.DIRECTION_SEQUENCE_INVALID,
            )
        price_rules_ok = _motive_hard_price_rules_pass(graph, parent)
        _check(
            checks,
            scope="motive_price_rules",
            required=direction_applicable,
            applicable=direction_applicable,
            passed=price_rules_ok if direction_applicable else None,
            reason=VerificationReasonCode.HARD_PRICE_RULE_FAILURE,
            details=_motive_hard_price_rule_details(graph, parent),
        )
        diagonal = graph.declared_family == "diagonal"
        diagonal_geometry_ok = True
        if diagonal and graph.diagonal_declaration is None:
            diagonal_geometry_ok = False
            _check(
                checks,
                scope="diagonal_declaration",
                required=True,
                applicable=True,
                passed=False,
                reason=VerificationReasonCode.DIAGONAL_DECLARATION_REQUIRED,
            )
        elif diagonal:
            children = {child.sequence_position: child for child in graph.children}
            try:
                geometry = calculate_diagonal_geometry(
                    (
                        children["1"].start_pivot.price,
                        children["1"].end_pivot.price,
                        children["2"].end_pivot.price,
                        children["3"].end_pivot.price,
                        children["4"].end_pivot.price,
                        children["5"].end_pivot.price,
                    ),
                    (
                        children["1"].start_pivot.timestamp_utc,
                        children["1"].end_pivot.timestamp_utc,
                        children["2"].end_pivot.timestamp_utc,
                        children["3"].end_pivot.timestamp_utc,
                        children["4"].end_pivot.timestamp_utc,
                        children["5"].end_pivot.timestamp_utc,
                    ),
                    direction=parent.direction,
                    declaration=graph.diagonal_declaration,
                    child_families=tuple(
                        children[position].declared_family
                        for position in ("1", "2", "3", "4", "5")
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                diagonal_geometry_ok = False
                _check(
                    checks,
                    scope="diagonal_geometry",
                    required=True,
                    applicable=True,
                    passed=False,
                    reason=VerificationReasonCode.DIAGONAL_GEOMETRY_FAILURE,
                    details={"error": str(exc)},
                )
            else:
                diagonal_geometry_ok = geometry.all_hard_rules_passed
                family_pass = geometry.child_family_rules_passed
                _check(
                    checks,
                    scope="diagonal_child_families",
                    required=True,
                    applicable=True,
                    passed=family_pass,
                    reason=VerificationReasonCode.DIAGONAL_CHILD_FAMILY_INVALID,
                    details=geometry.to_dict(),
                )
                _check(
                    checks,
                    scope="diagonal_geometry",
                    required=True,
                    applicable=True,
                    passed=(
                        geometry.geometry_passed
                        and geometry.wave_4_overlap_passed
                        and geometry.wave_5_termination_passed
                    ),
                    reason=VerificationReasonCode.DIAGONAL_GEOMETRY_FAILURE,
                    details=geometry.to_dict(),
                )

        for child in graph.children:
            if child.invalidation is None:
                continue
            invalidation_known = child.invalidation.source_pivot_id in catalog
            _check(
                checks,
                scope=f"invalidation_source:{child.child_id}",
                required=False,
                applicable=True,
                passed=invalidation_known,
                reason=VerificationReasonCode.INVALIDATION_PIVOT_UNKNOWN,
                evidence_ids=(child.invalidation.source_pivot_id,),
                details={"structural_elliott_invalidation": False},
            )
            if invalidation_known and full_coverage and window.is_native:
                _check(
                    checks,
                    scope=f"invalidation_eval:{child.child_id}",
                    required=False,
                    applicable=True,
                    passed=not _invalidation_breached(child, window),
                    reason=VerificationReasonCode.INVALIDATION_BREACHED,
                    evidence_ids=(child.invalidation.source_pivot_id, window.window_id),
                    details={"structural_elliott_invalidation": False},
                )

        terminal_reached = graph.target_child_degree == request.verifier_policy.terminal_degree
        _check(
            checks,
            scope="terminal_degree",
            required=True,
            applicable=True,
            passed=terminal_reached,
            reason=VerificationReasonCode.TERMINAL_DEGREE_NOT_REACHED,
            details={"target_child_degree": graph.target_child_degree, "terminal_degree": request.verifier_policy.terminal_degree},
        )

        status = _result_status(
            checks,
            terminal_reached=terminal_reached and (not diagonal or diagonal_geometry_ok),
        )
        reason_codes = tuple(
            dict.fromkeys(
                check.reason_code
                for check in checks
                if check.applicable and check.passed is not True
            )
        )
        if status is SubdivisionVerificationStatus.VERIFIED:
            reason_codes = tuple(
                dict.fromkeys(
                    (
                        VerificationReasonCode.TERMINAL_LEAF_VERIFIED,
                        *(
                            check.reason_code
                            for check in checks
                            if not check.required
                            and check.applicable
                            and check.passed is not True
                        ),
                    )
                )
            )
        scope = VerificationProofScope(
            target_timeframe=graph.target_timeframe,
            verified_to_timeframe=graph.target_timeframe if status is SubdivisionVerificationStatus.VERIFIED else None,
            terminal_degree=request.verifier_policy.terminal_degree,
            recursion_depth=parent.generation_depth + 1,
            coverage_limitations=graph.proof_scope.coverage_limitations,
        )
        child_results = tuple(
            self._child_result(child, checks, status, terminal_reached)
            for child in graph.children
        )
        warnings: list[str] = []
        if status is not SubdivisionVerificationStatus.VERIFIED:
            warnings.append("Raw candidate or model prose is not a verification result.")
        return SubdivisionVerifierResult.create(
            result_id="",
            verification_request_id=request.request_id,
            verification_request_hash=request.content_hash,
            graph_id=graph.graph_id,
            graph_content_hash=graph.content_hash,
            status=status,
            proof_scope=scope,
            child_results=child_results,
            checks=tuple(checks),
            reason_codes=reason_codes,
            required_next_evidence=_required_next_evidence(reason_codes),
            warnings=tuple(warnings),
            created_at_utc=request.created_at_utc,
        )

    @staticmethod
    def _child_result(
        child: GeneratedChildWave,
        checks: Sequence[SubdivisionCheck],
        graph_status: SubdivisionVerificationStatus,
        terminal_reached: bool,
    ) -> ChildSubdivisionVerification:
        relevant = [
            check
            for check in checks
            if child.child_id in check.scope
            or child.start_pivot.pivot_id in check.evidence_ids
            or child.end_pivot.pivot_id in check.evidence_ids
            or (
                child.invalidation is not None
                and child.invalidation.source_pivot_id in check.evidence_ids
            )
        ]
        if graph_status is SubdivisionVerificationStatus.NOT_COVERED:
            status = SubdivisionVerificationStatus.NOT_COVERED
        elif any(check.required and check.passed is False for check in relevant):
            status = SubdivisionVerificationStatus.INCONSISTENT
        elif graph_status is SubdivisionVerificationStatus.INCONSISTENT:
            status = SubdivisionVerificationStatus.INCONSISTENT
        elif graph_status is SubdivisionVerificationStatus.UNPROVEN or not terminal_reached:
            status = SubdivisionVerificationStatus.UNPROVEN
        else:
            status = SubdivisionVerificationStatus.VERIFIED
        codes = tuple(
            dict.fromkeys(
                check.reason_code
                for check in relevant
                if check.applicable and check.passed is not True
            )
        )
        if status is SubdivisionVerificationStatus.VERIFIED:
            codes = tuple(
                dict.fromkeys(
                    (
                        VerificationReasonCode.TERMINAL_LEAF_VERIFIED,
                        *(
                            check.reason_code
                            for check in relevant
                            if not check.required
                            and check.applicable
                            and check.passed is not True
                        ),
                    )
                )
            )
        return ChildSubdivisionVerification(
            child_id=child.child_id,
            status=status,
            reason_codes=codes,
            check_ids=tuple(check.check_id for check in relevant),
        )

    def verify_pair(
        self,
        primary_request: LowerTimeframeSubdivisionVerificationRequest,
        alternative_request: LowerTimeframeSubdivisionVerificationRequest,
        *,
        shadow_mode: bool = False,
    ) -> SubdivisionVerificationPair:
        """Verify each graph independently; no result can erase its alternate."""
        primary = self.verify(primary_request, shadow_mode=shadow_mode)
        alternative = self.verify(alternative_request, shadow_mode=shadow_mode)
        return SubdivisionVerificationPair.create(
            pair_id="",
            primary_result=primary,
            alternative_result=alternative,
            created_at_utc=max(primary.created_at_utc, alternative.created_at_utc),
        )


__all__ = [
    "ChildSubdivisionVerification",
    "LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_CALCULATION_VERSION",
    "LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_POLICY_VERSION",
    "LOWER_TIMEFRAME_SUBDIVISION_VERIFIER_SCHEMA_VERSION",
    "LowerTimeframeSubdivisionVerificationRequest",
    "LowerTimeframeSubdivisionVerifier",
    "NextEvidenceCode",
    "SubdivisionCheck",
    "SubdivisionVerificationPair",
    "SubdivisionVerificationStatus",
    "SubdivisionVerifierPolicy",
    "SubdivisionVerifierResult",
    "VerificationProofScope",
    "VerificationReasonCode",
    "build_subdivision_verification_request",
    "googl_intermediate_to_minute_verifier_policy",
    "subdivision_verification_pair_content_hash",
    "subdivision_verification_request_content_hash",
    "subdivision_verifier_policy_content_hash",
    "subdivision_verifier_result_content_hash",
    "validate_subdivision_verification_request",
]
