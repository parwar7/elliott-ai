"""Shadow-only recursive proof bundles for lower-timeframe Elliott evidence.

This module is deliberately an in-memory sidecar.  It receives only frozen
candidate graphs and native OHLCV windows.  It never constructs a provider,
fetches candles, calls a model, writes SQLite, changes a stored resolution, or
decides which Elliott hypothesis is correct.

The proof policy is intentionally narrow: monthly -> weekly -> daily -> native
4h.  A parent is verified only after every required descendant has been
verified bottom-up.  Candidate graphs are untrusted structural assertions;
they have no route to set a verification status.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .correction_semantics import CORRECTIVE_FAMILIES, MOTIVE_FAMILIES
from .forecast_records import DatasetCutoff, canonical_sha256
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGraphRole,
    CandidateInvalidation,
    GeneratedChildWave,
    InvalidationDirection,
    InvalidationEvaluationBasis,
    NativeOHLCVWindow,
    ParentCandidateSnapshot,
    PivotPriceField,
    TypedPivot,
    candidate_child_graph_content_hash,
    expected_child_positions,
    family_children_are_admissible,
    native_window_content_hash,
    native_window_rows_hash,
    parent_candidate_content_hash,
)
from .market_data_identity import dataset_feed_family_matches
from .lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus
from .schema import STANDARD_DEGREES


RECURSIVE_MULTI_TIMEFRAME_PROOF_SCHEMA_VERSION = "recursive-multi-timeframe-proof-1.0.0"
RECURSIVE_MULTI_TIMEFRAME_PROOF_POLICY_VERSION = "recursive-multi-timeframe-proof-policy-1.0.0"
RECURSIVE_MULTI_TIMEFRAME_PROOF_CALCULATION_VERSION = "recursive-multi-timeframe-proof-calculation-1.0.0"
PROOF_COVERAGE_ATTESTATION_SCHEMA_VERSION = "recursive-proof-coverage-attestation-1.0.0"
PROOF_WAVE_SNAPSHOT_SCHEMA_VERSION = "recursive-proof-wave-snapshot-1.0.0"
PROOF_BOUNDARY_LINEAGE_SCHEMA_VERSION = "recursive-proof-boundary-lineage-1.0.0"
RECURSIVE_PROOF_NODE_SCHEMA_VERSION = "recursive-proof-node-1.1.0"
RECURSIVE_PROOF_BUNDLE_SCHEMA_VERSION = "recursive-proof-bundle-1.0.0"
RECURSIVE_PROOF_RESULT_SCHEMA_VERSION = "recursive-proof-result-1.0.0"

RECURSIVE_PROOF_TIMEFRAME_LADDER = ("monthly", "weekly", "daily", "4h")
_ALLOWED_FAMILIES = frozenset((*MOTIVE_FAMILIES, *CORRECTIVE_FAMILIES))
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_EPSILON = 1e-9


class RecursiveProofHypothesisRole(StrEnum):
    """The independent candidate trees a caller may prove side by side."""

    PRIMARY = "primary"
    ALTERNATIVE = "alternative"


class RecursiveProofReasonCode(StrEnum):
    """Controlled reasons for a recursive proof result.

    Statuses are intentionally not inferred from prose.  The reason codes make
    it clear whether a result failed because of an actual contradiction, a
    missing proof branch, or missing compatible historical data.
    """

    SHADOW_MODE_REQUIRED = "shadow_mode_required"
    SOURCE_INTEGRITY_FAILED = "source_integrity_failed"
    BUNDLE_GRAPH_INVALID = "bundle_graph_invalid"
    ROOT_TIMEFRAME_INVALID = "root_timeframe_invalid"
    PROOF_LADDER_INVALID = "proof_ladder_invalid"
    PROOF_TIMEFRAME_SKIPPED = "proof_timeframe_skipped"
    REQUIRED_DESCENDANT_MISSING = "required_descendant_missing"
    UNEXPECTED_TERMINAL_DESCENDANT = "unexpected_terminal_descendant"
    NODE_LIMIT_EXHAUSTED = "node_limit_exhausted"
    PARENT_NOT_COMPLETED = "parent_not_completed"
    REQUIRED_NATIVE_WINDOW_MISSING = "required_native_window_missing"
    REQUIRED_COVERAGE_ATTESTATION_MISSING = "required_coverage_attestation_missing"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    COVERAGE_INTERVAL_INSUFFICIENT = "coverage_interval_insufficient"
    REQUIRED_WINDOW_NOT_NATIVE = "required_window_not_native"
    WINDOW_METADATA_INCOMPATIBLE = "window_metadata_incompatible"
    WINDOW_AFTER_CUTOFF = "window_after_cutoff"
    INCOMPLETE_CANDLE = "incomplete_candle"
    PIVOT_NOT_CATALOGUED = "pivot_not_catalogued"
    PIVOT_LINEAGE_MISMATCH = "pivot_lineage_mismatch"
    PIVOT_VALUE_MISMATCH = "pivot_value_mismatch"
    PARENT_CHILD_BOUNDARY_MISMATCH = "parent_child_boundary_mismatch"
    CHILD_PIVOT_CHAIN_DISCONTINUOUS = "child_pivot_chain_discontinuous"
    CHILD_SNAPSHOT_MISMATCH = "child_snapshot_mismatch"
    CHILD_ORDER_INVALID = "child_order_invalid"
    FAMILY_CHILDREN_INCOMPATIBLE = "family_children_incompatible"
    DIRECTION_SEQUENCE_INVALID = "direction_sequence_invalid"
    HARD_PRICE_RULE_FAILURE = "hard_price_rule_failure"
    DIAGONAL_GEOMETRY_UNPROVEN = "diagonal_geometry_unproven"
    INVALIDATION_PIVOT_UNKNOWN = "invalidation_pivot_unknown"
    INVALIDATION_BREACHED = "invalidation_breached"
    CANDIDATE_STATUS_FORBIDDEN = "candidate_status_forbidden"
    TERMINAL_4H_VERIFIED = "terminal_4h_verified"
    DERIVED_4H_BINDING_INVALID = "derived_4h_binding_invalid"
    DERIVED_4H_POLICY_MISMATCH = "derived_4h_policy_mismatch"
    DERIVED_4H_SOURCE_NOT_COVERED = "derived_4h_source_not_covered"
    DERIVED_4H_SOURCE_INCONSISTENT = "derived_4h_source_inconsistent"
    TERMINAL_DERIVED_4H_VERIFIED = "terminal_derived_4h_verified"


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


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, model_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


def _string_tuple(value: Sequence[str], *, field_name: str, sort_unique: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} cannot contain duplicates.")
    return tuple(sorted(result)) if sort_unique else result


def _coerce(value: Any, model_type: type[Any], *, field_name: str) -> Any:
    if isinstance(value, model_type):
        return value
    if isinstance(value, Mapping):
        parser = getattr(model_type, "from_dict", None)
        if callable(parser):
            return parser(value)
    raise TypeError(f"{field_name} must contain {model_type.__name__} values.")


def _coerce_optional(value: Any, model_type: type[Any], *, field_name: str) -> Any:
    if value is None:
        return None
    return _coerce(value, model_type, field_name=field_name)


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
class ProofCoverageAttestation(_JsonContract):
    """Explicit full-coverage assertion for one supplied native interval.

    Native timestamps alone cannot prove that an omitted historical interval was
    a valid exchange closure rather than a missing download.  A caller must
    therefore provide this immutable coverage fact for each proof step.
    """

    attestation_id: str
    proof_timeframe: str
    dataset_id: str
    source_window_hash: str
    interval_start_utc: str
    interval_end_utc: str
    coverage_complete: bool
    missing_interval_ids: tuple[str, ...]
    coverage_method: str
    schema_version: str = PROOF_COVERAGE_ATTESTATION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("attestation_id", "proof_timeframe", "dataset_id", "coverage_method", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if self.proof_timeframe not in RECURSIVE_PROOF_TIMEFRAME_LADDER:
            raise ValueError("proof_timeframe is not in the fixed recursive proof ladder.")
        object.__setattr__(self, "source_window_hash", _require_hash(self.source_window_hash, field_name="source_window_hash"))
        object.__setattr__(self, "interval_start_utc", _normalize_utc(self.interval_start_utc, field_name="interval_start_utc"))
        object.__setattr__(self, "interval_end_utc", _normalize_utc(self.interval_end_utc, field_name="interval_end_utc"))
        if _utc(self.interval_end_utc) <= _utc(self.interval_start_utc):
            raise ValueError("Coverage interval end must be after its start.")
        if not isinstance(self.coverage_complete, bool):
            raise TypeError("coverage_complete must be boolean.")
        object.__setattr__(self, "missing_interval_ids", _string_tuple(self.missing_interval_ids, field_name="missing_interval_ids", sort_unique=True))
        if self.coverage_complete and self.missing_interval_ids:
            raise ValueError("A complete coverage attestation cannot list missing intervals.")
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != proof_coverage_attestation_content_hash(self):
            raise ValueError("ProofCoverageAttestation content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ProofCoverageAttestation":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("attestation_id"):
            values["attestation_id"] = f"coverage_{canonical_sha256({'dataset_id': values.get('dataset_id'), 'timeframe': values.get('proof_timeframe'), 'start': values.get('interval_start_utc'), 'end': values.get('interval_end_utc')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=proof_coverage_attestation_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ProofCoverageAttestation":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != proof_coverage_attestation_content_hash(result):
            raise ValueError("ProofCoverageAttestation content_hash does not match its payload.")
        return result


def proof_coverage_attestation_content_hash(value: ProofCoverageAttestation | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class ProofWaveSnapshot(_JsonContract):
    """A wave assertion whose Elliott degree is intentionally separate from proof time."""

    wave_id: str
    degree: str
    declared_family: str
    direction: str
    completion_state: str
    start_pivot: TypedPivot
    end_pivot: TypedPivot
    invalidation: CandidateInvalidation
    source_reference_hash: str
    source_kind: str
    schema_version: str = PROOF_WAVE_SNAPSHOT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("wave_id", "source_kind", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        if self.degree not in STANDARD_DEGREES or self.degree == "Unassigned":
            raise ValueError("Proof wave degree must be an assigned standard degree.")
        family = _require_text(self.declared_family, field_name="declared_family").lower()
        if family not in _ALLOWED_FAMILIES:
            raise ValueError("Proof wave declared_family is unsupported.")
        object.__setattr__(self, "declared_family", family)
        direction = _require_text(self.direction, field_name="direction")
        if direction not in {"up", "down"}:
            raise ValueError("Proof wave direction must be up or down.")
        object.__setattr__(self, "direction", direction)
        completion = _require_text(self.completion_state, field_name="completion_state")
        if completion not in {"completed", "active", "projected"}:
            raise ValueError("Proof wave completion_state is invalid.")
        object.__setattr__(self, "completion_state", completion)
        object.__setattr__(self, "start_pivot", _coerce(self.start_pivot, TypedPivot, field_name="start_pivot"))
        object.__setattr__(self, "end_pivot", _coerce(self.end_pivot, TypedPivot, field_name="end_pivot"))
        if _utc(self.end_pivot.timestamp_utc) <= _utc(self.start_pivot.timestamp_utc):
            raise ValueError("Proof wave end pivot must be after its start pivot.")
        object.__setattr__(self, "invalidation", _coerce(self.invalidation, CandidateInvalidation, field_name="invalidation"))
        object.__setattr__(self, "source_reference_hash", _require_hash(self.source_reference_hash, field_name="source_reference_hash"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != proof_wave_snapshot_content_hash(self):
            raise ValueError("ProofWaveSnapshot content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ProofWaveSnapshot":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=proof_wave_snapshot_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ProofWaveSnapshot":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != proof_wave_snapshot_content_hash(result):
            raise ValueError("ProofWaveSnapshot content_hash does not match its payload.")
        return result


def proof_wave_snapshot_content_hash(value: ProofWaveSnapshot | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class ProofBoundaryLineage(_JsonContract):
    """An explicit exact-price parent/child boundary mapping.

    Parent and child proof timeframes can use different candle timestamps.  A
    lineage record keeps that difference visible without weakening the price,
    source-row, or typed-pivot requirements at either side of the boundary.
    The complete-history runner is responsible for proving the source-candle
    bracketing semantics before it supplies this immutable mapping.
    """

    lineage_id: str
    parent_pivot: TypedPivot
    child_pivot: TypedPivot
    mapping_kind: str
    schema_version: str = PROOF_BOUNDARY_LINEAGE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "lineage_id", _require_text(self.lineage_id, field_name="lineage_id"))
        object.__setattr__(self, "parent_pivot", _coerce(self.parent_pivot, TypedPivot, field_name="parent_pivot"))
        object.__setattr__(self, "child_pivot", _coerce(self.child_pivot, TypedPivot, field_name="child_pivot"))
        if self.parent_pivot.timeframe == self.child_pivot.timeframe:
            raise ValueError("Boundary lineage must connect different proof timeframes.")
        if abs(self.parent_pivot.price - self.child_pivot.price) > _EPSILON:
            raise ValueError("Boundary lineage requires an exact parent/child price match.")
        object.__setattr__(self, "mapping_kind", _require_text(self.mapping_kind, field_name="mapping_kind"))
        if self.mapping_kind != "exact_price_source_candle_bracket":
            raise ValueError("Boundary lineage mapping_kind is unsupported.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != proof_boundary_lineage_content_hash(self):
            raise ValueError("ProofBoundaryLineage content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ProofBoundaryLineage":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("lineage_id"):
            values["lineage_id"] = "boundary_lineage_" + canonical_sha256(
                {
                    "parent_pivot": _json_value(values.get("parent_pivot")),
                    "child_pivot": _json_value(values.get("child_pivot")),
                    "mapping_kind": values.get("mapping_kind"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=proof_boundary_lineage_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ProofBoundaryLineage":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != proof_boundary_lineage_content_hash(result):
            raise ValueError("ProofBoundaryLineage content_hash does not match its payload.")
        return result


def proof_boundary_lineage_content_hash(value: ProofBoundaryLineage | Mapping[str, Any]) -> str:
    return _hash_payload(value)


def proof_wave_from_parent_candidate(parent: ParentCandidateSnapshot) -> ProofWaveSnapshot:
    """Adapt a frozen existing parent without making proof time equal its degree."""

    if parent.content_hash != parent_candidate_content_hash(parent):
        raise ValueError("ParentCandidateSnapshot content hash does not match its immutable payload.")
    return ProofWaveSnapshot.create(
        wave_id=parent.parent_candidate_id,
        degree=parent.degree,
        declared_family=parent.declared_family,
        direction=parent.direction,
        completion_state=parent.completion_state,
        start_pivot=parent.start_pivot,
        end_pivot=parent.end_pivot,
        invalidation=parent.invalidation,
        source_reference_hash=parent.content_hash,
        source_kind="parent_candidate_snapshot",
    )


def proof_wave_from_generated_child(child: GeneratedChildWave, *, source_reference_hash: str) -> ProofWaveSnapshot:
    """Adapt a child candidate while preserving its candidate-only origin."""

    return ProofWaveSnapshot.create(
        wave_id=child.child_id,
        degree=child.degree,
        declared_family=child.declared_family,
        direction=child.direction,
        completion_state="completed",
        start_pivot=child.start_pivot,
        end_pivot=child.end_pivot,
        invalidation=child.invalidation,
        source_reference_hash=source_reference_hash,
        source_kind="generated_child_candidate",
    )


@dataclass(frozen=True, slots=True)
class RecursiveProofNode(_JsonContract):
    """One supplied proof interval.

    ``proof_timeframe`` is a verification-evidence property.  It is not
    derived from nor validated against the Elliott degree stored in ``wave``.
    This keeps a Cycle parent proved on monthly data distinct from the degree
    naming itself.
    """

    node_id: str
    wave: ProofWaveSnapshot
    proof_timeframe: str
    native_window: NativeOHLCVWindow | None
    native_pivot_catalog: tuple[TypedPivot, ...]
    coverage: ProofCoverageAttestation | None
    child_graph: CandidateChildGraph | None = None
    child_window: NativeOHLCVWindow | None = None
    child_pivot_catalog: tuple[TypedPivot, ...] = ()
    child_boundary_lineage: tuple[ProofBoundaryLineage, ...] = ()
    child_coverage: ProofCoverageAttestation | None = None
    child_node_ids: tuple[str, ...] = ()
    schema_version: str = RECURSIVE_PROOF_NODE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _require_text(self.node_id, field_name="node_id"))
        object.__setattr__(self, "wave", _coerce(self.wave, ProofWaveSnapshot, field_name="wave"))
        object.__setattr__(self, "proof_timeframe", _require_text(self.proof_timeframe, field_name="proof_timeframe"))
        if self.proof_timeframe not in RECURSIVE_PROOF_TIMEFRAME_LADDER:
            raise ValueError("proof_timeframe is not in the fixed recursive proof ladder.")
        object.__setattr__(self, "native_window", _coerce_optional(self.native_window, NativeOHLCVWindow, field_name="native_window"))
        if isinstance(self.native_pivot_catalog, (str, bytes)) or not isinstance(self.native_pivot_catalog, Sequence):
            raise TypeError("native_pivot_catalog must be a sequence.")
        native_catalog = tuple(_coerce(item, TypedPivot, field_name="native_pivot_catalog") for item in self.native_pivot_catalog)
        if len({item.pivot_id for item in native_catalog}) != len(native_catalog):
            raise ValueError("native_pivot_catalog cannot contain duplicate IDs.")
        object.__setattr__(self, "native_pivot_catalog", native_catalog)
        object.__setattr__(self, "coverage", _coerce_optional(self.coverage, ProofCoverageAttestation, field_name="coverage"))
        object.__setattr__(self, "child_graph", _coerce_optional(self.child_graph, CandidateChildGraph, field_name="child_graph"))
        object.__setattr__(self, "child_window", _coerce_optional(self.child_window, NativeOHLCVWindow, field_name="child_window"))
        if isinstance(self.child_pivot_catalog, (str, bytes)) or not isinstance(self.child_pivot_catalog, Sequence):
            raise TypeError("child_pivot_catalog must be a sequence.")
        child_catalog = tuple(_coerce(item, TypedPivot, field_name="child_pivot_catalog") for item in self.child_pivot_catalog)
        if len({item.pivot_id for item in child_catalog}) != len(child_catalog):
            raise ValueError("child_pivot_catalog cannot contain duplicate IDs.")
        object.__setattr__(self, "child_pivot_catalog", child_catalog)
        if isinstance(self.child_boundary_lineage, (str, bytes)) or not isinstance(self.child_boundary_lineage, Sequence):
            raise TypeError("child_boundary_lineage must be a sequence.")
        boundary_lineage = tuple(
            _coerce(item, ProofBoundaryLineage, field_name="child_boundary_lineage")
            for item in self.child_boundary_lineage
        )
        if len({item.lineage_id for item in boundary_lineage}) != len(boundary_lineage):
            raise ValueError("child_boundary_lineage cannot contain duplicate IDs.")
        object.__setattr__(self, "child_boundary_lineage", boundary_lineage)
        object.__setattr__(self, "child_coverage", _coerce_optional(self.child_coverage, ProofCoverageAttestation, field_name="child_coverage"))
        object.__setattr__(self, "child_node_ids", _string_tuple(self.child_node_ids, field_name="child_node_ids"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_node_content_hash(self):
            raise ValueError("RecursiveProofNode content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofNode":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_node_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RecursiveProofNode":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != recursive_proof_node_content_hash(result):
            raise ValueError("RecursiveProofNode content_hash does not match its payload.")
        return result


def recursive_proof_node_content_hash(value: RecursiveProofNode | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, RecursiveProofNode) else dict(value)
    payload.pop("content_hash", None)
    # Keep earlier immutable proof nodes readable.  They predate explicit
    # cross-timeframe boundary lineage and therefore could not have carried it.
    if payload.get("schema_version") == "recursive-proof-node-1.0.0":
        payload.pop("child_boundary_lineage", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class RecursiveProofPolicy(_JsonContract):
    """Versioned, fixed proof policy; it cannot skip a required rung."""

    policy_id: str
    timeframe_ladder: tuple[str, ...]
    maximum_nodes_per_hypothesis: int
    minimum_native_bars: int = 2
    schema_version: str = RECURSIVE_MULTI_TIMEFRAME_PROOF_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _require_text(self.policy_id, field_name="policy_id"))
        object.__setattr__(self, "timeframe_ladder", _string_tuple(self.timeframe_ladder, field_name="timeframe_ladder"))
        if self.timeframe_ladder != RECURSIVE_PROOF_TIMEFRAME_LADDER:
            raise ValueError("Recursive proof policy must use monthly -> weekly -> daily -> native 4h exactly.")
        if not isinstance(self.maximum_nodes_per_hypothesis, int) or isinstance(self.maximum_nodes_per_hypothesis, bool) or self.maximum_nodes_per_hypothesis < 1:
            raise ValueError("maximum_nodes_per_hypothesis must be a positive integer.")
        if not isinstance(self.minimum_native_bars, int) or isinstance(self.minimum_native_bars, bool) or self.minimum_native_bars < 2:
            raise ValueError("minimum_native_bars must be at least two.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_policy_content_hash(self):
            raise ValueError("RecursiveProofPolicy content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofPolicy":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_policy_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RecursiveProofPolicy":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != recursive_proof_policy_content_hash(result):
            raise ValueError("RecursiveProofPolicy content_hash does not match its payload.")
        return result


def recursive_proof_policy_content_hash(value: RecursiveProofPolicy | Mapping[str, Any]) -> str:
    return _hash_payload(value)


def monthly_to_4h_recursive_proof_policy() -> RecursiveProofPolicy:
    """Return the only initial approved proof ladder.

    The default cap is deliberately well above a complete 5-5-5 motive tree.
    It is a per-hypothesis resource guard, never a terminal-proof condition.
    """

    return RecursiveProofPolicy.create(
        policy_id="monthly-weekly-daily-native-4h-v1",
        timeframe_ladder=RECURSIVE_PROOF_TIMEFRAME_LADDER,
        maximum_nodes_per_hypothesis=200,
        minimum_native_bars=2,
    )


@dataclass(frozen=True, slots=True)
class RecursiveProofBundle(_JsonContract):
    """One immutable Primary or Alternative proof tree.

    A bundle has exactly one root.  The constructor freezes the supplied nodes
    but leaves structural mismatches for the deterministic verifier to report
    rather than silently repairing them.
    """

    bundle_id: str
    role: RecursiveProofHypothesisRole
    source_analysis_run_id: int
    source_analysis_run_hash: str
    source_degree_resolution_id: int
    source_degree_resolution_hash: str
    analysis_cutoff_utc: str
    root_node_id: str
    nodes: tuple[RecursiveProofNode, ...]
    policy: RecursiveProofPolicy
    shadow_mode: bool
    created_at_utc: str
    schema_version: str = RECURSIVE_PROOF_BUNDLE_SCHEMA_VERSION
    calculation_version: str = RECURSIVE_MULTI_TIMEFRAME_PROOF_CALCULATION_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_id", _require_text(self.bundle_id, field_name="bundle_id"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        for name in ("source_analysis_run_id", "source_degree_resolution_id"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        for name in ("source_analysis_run_hash", "source_degree_resolution_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "analysis_cutoff_utc", _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        object.__setattr__(self, "root_node_id", _require_text(self.root_node_id, field_name="root_node_id"))
        if isinstance(self.nodes, (str, bytes)) or not isinstance(self.nodes, Sequence) or not self.nodes:
            raise ValueError("nodes must be a non-empty sequence.")
        nodes = tuple(_coerce(item, RecursiveProofNode, field_name="nodes") for item in self.nodes)
        if len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("Recursive proof nodes cannot have duplicate IDs.")
        if self.root_node_id not in {item.node_id for item in nodes}:
            raise ValueError("root_node_id must identify a supplied node.")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "policy", _coerce(self.policy, RecursiveProofPolicy, field_name="policy"))
        if not isinstance(self.shadow_mode, bool):
            raise TypeError("shadow_mode must be boolean.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        object.__setattr__(self, "calculation_version", _require_text(self.calculation_version, field_name="calculation_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_bundle_content_hash(self):
            raise ValueError("RecursiveProofBundle content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofBundle":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("bundle_id"):
            values["bundle_id"] = f"recursive_bundle_{canonical_sha256({'role': _json_value(values.get('role')), 'root': values.get('root_node_id'), 'created_at_utc': values.get('created_at_utc')})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_bundle_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RecursiveProofBundle":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != recursive_proof_bundle_content_hash(result):
            raise ValueError("RecursiveProofBundle content_hash does not match its payload.")
        return result


def recursive_proof_bundle_content_hash(value: RecursiveProofBundle | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class RecursiveProofBundlePair(_JsonContract):
    """Two isolated proof trees; neither one can promote or erase the other."""

    pair_id: str
    primary_bundle: RecursiveProofBundle
    alternative_bundle: RecursiveProofBundle
    created_at_utc: str
    schema_version: str = RECURSIVE_PROOF_BUNDLE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _require_text(self.pair_id, field_name="pair_id"))
        object.__setattr__(self, "primary_bundle", _coerce(self.primary_bundle, RecursiveProofBundle, field_name="primary_bundle"))
        object.__setattr__(self, "alternative_bundle", _coerce(self.alternative_bundle, RecursiveProofBundle, field_name="alternative_bundle"))
        if self.primary_bundle.role is not RecursiveProofHypothesisRole.PRIMARY:
            raise ValueError("primary_bundle must carry the primary role.")
        if self.alternative_bundle.role is not RecursiveProofHypothesisRole.ALTERNATIVE:
            raise ValueError("alternative_bundle must carry the alternative role.")
        if self.primary_bundle.bundle_id == self.alternative_bundle.bundle_id:
            raise ValueError("Primary and Alternative bundles must remain distinct immutable records.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_bundle_pair_content_hash(self):
            raise ValueError("RecursiveProofBundlePair content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofBundlePair":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("pair_id"):
            values["pair_id"] = f"recursive_pair_{canonical_sha256({'primary': _json_value(values.get('primary_bundle')), 'alternative': _json_value(values.get('alternative_bundle'))})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_bundle_pair_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RecursiveProofBundlePair":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != recursive_proof_bundle_pair_content_hash(result):
            raise ValueError("RecursiveProofBundlePair content_hash does not match its payload.")
        return result


def recursive_proof_bundle_pair_content_hash(value: RecursiveProofBundlePair | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class RecursiveProofNodeResult(_JsonContract):
    node_id: str
    proof_timeframe: str
    status: SubdivisionVerificationStatus
    reason_codes: tuple[RecursiveProofReasonCode, ...]
    child_node_ids: tuple[str, ...]
    verified_to_timeframe: str | None
    warnings: tuple[str, ...]
    schema_version: str = RECURSIVE_PROOF_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _require_text(self.node_id, field_name="node_id"))
        object.__setattr__(self, "proof_timeframe", _require_text(self.proof_timeframe, field_name="proof_timeframe"))
        if self.proof_timeframe not in RECURSIVE_PROOF_TIMEFRAME_LADDER:
            raise ValueError("proof_timeframe is not in the fixed recursive proof ladder.")
        object.__setattr__(self, "status", _enum(self.status, SubdivisionVerificationStatus, field_name="status"))
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        codes = tuple(_enum(item, RecursiveProofReasonCode, field_name="reason_codes") for item in self.reason_codes)
        object.__setattr__(self, "reason_codes", tuple(dict.fromkeys(codes)))
        object.__setattr__(self, "child_node_ids", _string_tuple(self.child_node_ids, field_name="child_node_ids"))
        if self.status is SubdivisionVerificationStatus.VERIFIED:
            if self.verified_to_timeframe != self.proof_timeframe:
                raise ValueError("A verified result must identify its own verified proof timeframe.")
        elif self.verified_to_timeframe is not None:
            raise ValueError("Only verified results may set verified_to_timeframe.")
        object.__setattr__(self, "warnings", _string_tuple(self.warnings, field_name="warnings", sort_unique=True))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_node_result_content_hash(self):
            raise ValueError("RecursiveProofNodeResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofNodeResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_node_result_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RecursiveProofNodeResult":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != recursive_proof_node_result_content_hash(result):
            raise ValueError("RecursiveProofNodeResult content_hash does not match its payload.")
        return result


def recursive_proof_node_result_content_hash(value: RecursiveProofNodeResult | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class RecursiveProofBundleResult(_JsonContract):
    bundle_id: str
    bundle_content_hash: str
    role: RecursiveProofHypothesisRole
    status: SubdivisionVerificationStatus
    root_node_id: str
    node_results: tuple[RecursiveProofNodeResult, ...]
    reason_codes: tuple[RecursiveProofReasonCode, ...]
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = RECURSIVE_PROOF_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_id", _require_text(self.bundle_id, field_name="bundle_id"))
        object.__setattr__(self, "bundle_content_hash", _require_hash(self.bundle_content_hash, field_name="bundle_content_hash"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        object.__setattr__(self, "status", _enum(self.status, SubdivisionVerificationStatus, field_name="status"))
        object.__setattr__(self, "root_node_id", _require_text(self.root_node_id, field_name="root_node_id"))
        if isinstance(self.node_results, (str, bytes)) or not isinstance(self.node_results, Sequence):
            raise TypeError("node_results must be a sequence.")
        results = tuple(_coerce(item, RecursiveProofNodeResult, field_name="node_results") for item in self.node_results)
        if len({item.node_id for item in results}) != len(results):
            raise ValueError("node_results cannot contain duplicate node IDs.")
        if self.root_node_id not in {item.node_id for item in results}:
            raise ValueError("node_results must contain the root node result.")
        root = next(item for item in results if item.node_id == self.root_node_id)
        if root.status is not self.status:
            raise ValueError("Bundle status must equal its root node status.")
        object.__setattr__(self, "node_results", results)
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        codes = tuple(_enum(item, RecursiveProofReasonCode, field_name="reason_codes") for item in self.reason_codes)
        object.__setattr__(self, "reason_codes", tuple(dict.fromkeys(codes)))
        object.__setattr__(self, "warnings", _string_tuple(self.warnings, field_name="warnings", sort_unique=True))
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_bundle_result_content_hash(self):
            raise ValueError("RecursiveProofBundleResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofBundleResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_bundle_result_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "RecursiveProofBundleResult":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != recursive_proof_bundle_result_content_hash(result):
            raise ValueError("RecursiveProofBundleResult content_hash does not match its payload.")
        return result


def recursive_proof_bundle_result_content_hash(value: RecursiveProofBundleResult | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class RecursiveProofBundlePairResult(_JsonContract):
    pair_id: str
    pair_content_hash: str
    primary_result: RecursiveProofBundleResult
    alternative_result: RecursiveProofBundleResult
    created_at_utc: str
    schema_version: str = RECURSIVE_PROOF_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _require_text(self.pair_id, field_name="pair_id"))
        object.__setattr__(self, "pair_content_hash", _require_hash(self.pair_content_hash, field_name="pair_content_hash"))
        object.__setattr__(self, "primary_result", _coerce(self.primary_result, RecursiveProofBundleResult, field_name="primary_result"))
        object.__setattr__(self, "alternative_result", _coerce(self.alternative_result, RecursiveProofBundleResult, field_name="alternative_result"))
        if self.primary_result.role is not RecursiveProofHypothesisRole.PRIMARY:
            raise ValueError("primary_result must carry the primary role.")
        if self.alternative_result.role is not RecursiveProofHypothesisRole.ALTERNATIVE:
            raise ValueError("alternative_result must carry the alternative role.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != recursive_proof_bundle_pair_result_content_hash(self):
            raise ValueError("RecursiveProofBundlePairResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "RecursiveProofBundlePairResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=recursive_proof_bundle_pair_result_content_hash(result))


def recursive_proof_bundle_pair_result_content_hash(value: RecursiveProofBundlePairResult | Mapping[str, Any]) -> str:
    return _hash_payload(value)


def _same_anchor(left: TypedPivot, right: TypedPivot) -> bool:
    return left.timestamp_utc == right.timestamp_utc and abs(left.price - right.price) <= _EPSILON


def _boundary_lineage_matches(
    node: RecursiveProofNode,
    graph: CandidateChildGraph,
) -> bool:
    """Validate explicit mapped boundaries without treating timestamps as equal.

    A mapping is allowed only when it preserves the exact typed parent pivot,
    the exact child pivot selected from the lower-timeframe catalog, and the
    same price.  The complete-history runner additionally validates source
    candle bracketing when it constructs the mapping.
    """

    if len(node.child_boundary_lineage) != 2 or not graph.children:
        return False
    expected = (
        (node.wave.start_pivot, graph.children[0].start_pivot),
        (node.wave.end_pivot, graph.children[-1].end_pivot),
    )
    for parent, child in expected:
        match = next(
            (
                item
                for item in node.child_boundary_lineage
                if item.parent_pivot.pivot_id == parent.pivot_id
                and item.child_pivot.pivot_id == child.pivot_id
            ),
            None,
        )
        if match is None:
            return False
        if (
            match.parent_pivot.content_hash != parent.content_hash
            or match.child_pivot.content_hash != child.content_hash
            or abs(match.parent_pivot.price - match.child_pivot.price) > _EPSILON
        ):
            return False
    return True


def _pivot_matches_native_row(pivot: TypedPivot, window: NativeOHLCVWindow) -> bool:
    if pivot.timeframe != window.timeframe or pivot.source_window_hash != window.native_rows_hash:
        return False
    row = next((item for item in window.candles if item.source_row_hash == pivot.source_bar_hash), None)
    if row is None or row.timestamp_utc != pivot.timestamp_utc:
        return False
    return abs(float(getattr(row, pivot.price_field.value)) - pivot.price) <= _EPSILON


def _invalidation_breached(invalidation: CandidateInvalidation, start_pivot: TypedPivot, window: NativeOHLCVWindow) -> bool:
    """Evaluate after the origin row so a valid origin cannot self-invalidate."""

    start = _utc(start_pivot.timestamp_utc)
    for candle in window.candles:
        if _utc(candle.timestamp_utc) <= start:
            continue
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


def _motive_hard_price_rules_pass(graph: CandidateChildGraph, parent: ProofWaveSnapshot) -> bool:
    if graph.declared_family not in MOTIVE_FAMILIES:
        return True
    children = {child.sequence_position: child for child in graph.children}
    if set(children) != {"1", "2", "3", "4", "5"}:
        return False
    p0 = children["1"].start_pivot.price
    p1 = children["1"].end_pivot.price
    p2 = children["2"].end_pivot.price
    p3 = children["3"].end_pivot.price
    p4 = children["4"].end_pivot.price
    p5 = children["5"].end_pivot.price
    if parent.direction == "up":
        if p2 < p0 or p3 <= p1:
            return False
        if graph.declared_family == "impulse" and p4 <= p1:
            return False
    else:
        if p2 > p0 or p3 >= p1:
            return False
        if graph.declared_family == "impulse" and p4 >= p1:
            return False
    lengths = (abs(p1 - p0), abs(p3 - p2), abs(p5 - p4))
    return not (lengths[1] < lengths[0] and lengths[1] < lengths[2])


def _aggregate_status(statuses: Sequence[SubdivisionVerificationStatus]) -> SubdivisionVerificationStatus:
    if any(item is SubdivisionVerificationStatus.INCONSISTENT for item in statuses):
        return SubdivisionVerificationStatus.INCONSISTENT
    if any(item is SubdivisionVerificationStatus.NOT_COVERED for item in statuses):
        return SubdivisionVerificationStatus.NOT_COVERED
    if any(item is SubdivisionVerificationStatus.UNPROVEN for item in statuses):
        return SubdivisionVerificationStatus.UNPROVEN
    return SubdivisionVerificationStatus.VERIFIED


class RecursiveMultiTimeframeProofVerifier:
    """Deterministically verify one full monthly-to-4h proof bundle.

    No provider argument exists by design.  The verifier only sees immutable
    data already supplied in a bundle and works from native 4h leaves upward.
    """

    def verify(self, bundle: RecursiveProofBundle, *, shadow_mode: bool = False) -> RecursiveProofBundleResult:
        if not isinstance(bundle, RecursiveProofBundle):
            raise TypeError("bundle must be a RecursiveProofBundle.")
        node_by_id = {item.node_id: item for item in bundle.nodes}
        static_reasons = self._bundle_static_reasons(bundle, node_by_id, shadow_mode=shadow_mode)
        root = node_by_id[bundle.root_node_id]
        root_window = root.native_window
        root_dataset = root_window.dataset_cutoff if root_window is not None else None
        results: dict[str, RecursiveProofNodeResult] = {}
        visiting: set[str] = set()

        def walk(node_id: str) -> RecursiveProofNodeResult:
            existing = results.get(node_id)
            if existing is not None:
                return existing
            node = node_by_id.get(node_id)
            if node is None:
                # The caller only sees an unproven parent for a missing edge;
                # this branch is defensive for malformed/dangling input.
                return RecursiveProofNodeResult.create(
                    node_id=node_id,
                    proof_timeframe="4h",
                    status=SubdivisionVerificationStatus.UNPROVEN,
                    reason_codes=(RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING,),
                    child_node_ids=(),
                    verified_to_timeframe=None,
                    warnings=("Required descendant node was not supplied.",),
                )
            if node_id in visiting:
                result = self._result_for(
                    node,
                    status=SubdivisionVerificationStatus.INCONSISTENT,
                    reasons=(RecursiveProofReasonCode.BUNDLE_GRAPH_INVALID,),
                    child_node_ids=node.child_node_ids,
                )
                results[node_id] = result
                return result
            visiting.add(node_id)
            result = self._verify_node(
                bundle,
                node,
                node_by_id=node_by_id,
                root_dataset=root_dataset,
                child_results=walk,
            )
            visiting.remove(node_id)
            results[node_id] = result
            return result

        root_result = walk(bundle.root_node_id)
        if static_reasons:
            status = self._status_for_static_reasons(static_reasons, root_result.status)
            combined = tuple(dict.fromkeys((*root_result.reason_codes, *static_reasons)))
            root_result = self._result_for(
                root,
                status=status,
                reasons=combined,
                child_node_ids=root_result.child_node_ids,
                warnings=(*root_result.warnings, "Bundle-level deterministic validation prevented promotion from candidate evidence."),
            )
            results[root.node_id] = root_result
        ordered_results = tuple(
            results[item.node_id]
            for item in self._bottom_up_node_order(bundle, node_by_id)
            if item.node_id in results
        )
        # A normal result has the root included, but malformed cyclic input may
        # only have the root defensive result.  Preserve determinism either way.
        if root_result.node_id not in {item.node_id for item in ordered_results}:
            ordered_results = (*ordered_results, root_result)
        return RecursiveProofBundleResult.create(
            bundle_id=bundle.bundle_id,
            bundle_content_hash=bundle.content_hash,
            role=bundle.role,
            status=root_result.status,
            root_node_id=bundle.root_node_id,
            node_results=ordered_results,
            reason_codes=root_result.reason_codes,
            warnings=tuple(dict.fromkeys((*root_result.warnings, "Candidate graph labels and model prose never determine this status."))),
            created_at_utc=bundle.created_at_utc,
        )

    def verify_pair(self, pair: RecursiveProofBundlePair, *, shadow_mode: bool = False) -> RecursiveProofBundlePairResult:
        if not isinstance(pair, RecursiveProofBundlePair):
            raise TypeError("pair must be a RecursiveProofBundlePair.")
        primary = self.verify(pair.primary_bundle, shadow_mode=shadow_mode)
        alternative = self.verify(pair.alternative_bundle, shadow_mode=shadow_mode)
        return RecursiveProofBundlePairResult.create(
            pair_id=pair.pair_id,
            pair_content_hash=pair.content_hash,
            primary_result=primary,
            alternative_result=alternative,
            created_at_utc=max(primary.created_at_utc, alternative.created_at_utc),
        )

    @staticmethod
    def _status_for_static_reasons(
        reasons: Sequence[RecursiveProofReasonCode],
        current: SubdivisionVerificationStatus,
    ) -> SubdivisionVerificationStatus:
        if current is SubdivisionVerificationStatus.INCONSISTENT:
            return current
        if any(reason in {RecursiveProofReasonCode.REQUIRED_NATIVE_WINDOW_MISSING, RecursiveProofReasonCode.COVERAGE_INCOMPLETE, RecursiveProofReasonCode.COVERAGE_INTERVAL_INSUFFICIENT} for reason in reasons):
            return SubdivisionVerificationStatus.NOT_COVERED
        if any(reason in {RecursiveProofReasonCode.NODE_LIMIT_EXHAUSTED, RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING, RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED} for reason in reasons):
            return SubdivisionVerificationStatus.UNPROVEN
        return SubdivisionVerificationStatus.INCONSISTENT

    @staticmethod
    def _bottom_up_node_order(bundle: RecursiveProofBundle, node_by_id: Mapping[str, RecursiveProofNode]) -> tuple[RecursiveProofNode, ...]:
        order = {name: index for index, name in enumerate(bundle.policy.timeframe_ladder)}
        return tuple(sorted(bundle.nodes, key=lambda item: (-order[item.proof_timeframe], item.node_id)))

    def _bundle_static_reasons(
        self,
        bundle: RecursiveProofBundle,
        node_by_id: Mapping[str, RecursiveProofNode],
        *,
        shadow_mode: bool,
    ) -> tuple[RecursiveProofReasonCode, ...]:
        reasons: list[RecursiveProofReasonCode] = []
        if not shadow_mode or not bundle.shadow_mode:
            reasons.append(RecursiveProofReasonCode.SHADOW_MODE_REQUIRED)
        if bundle.content_hash != recursive_proof_bundle_content_hash(bundle):
            reasons.append(RecursiveProofReasonCode.SOURCE_INTEGRITY_FAILED)
        if bundle.policy.content_hash != recursive_proof_policy_content_hash(bundle.policy):
            reasons.append(RecursiveProofReasonCode.SOURCE_INTEGRITY_FAILED)
        if bundle.policy.timeframe_ladder != RECURSIVE_PROOF_TIMEFRAME_LADDER:
            reasons.append(RecursiveProofReasonCode.PROOF_LADDER_INVALID)
        root = node_by_id.get(bundle.root_node_id)
        if root is None or root.proof_timeframe != "monthly":
            reasons.append(RecursiveProofReasonCode.ROOT_TIMEFRAME_INVALID)
        if len(bundle.nodes) > bundle.policy.maximum_nodes_per_hypothesis:
            reasons.append(RecursiveProofReasonCode.NODE_LIMIT_EXHAUSTED)
        for node in bundle.nodes:
            if node.content_hash != recursive_proof_node_content_hash(node) or node.wave.content_hash != proof_wave_snapshot_content_hash(node.wave):
                reasons.append(RecursiveProofReasonCode.SOURCE_INTEGRITY_FAILED)
            if node.child_graph is not None:
                if node.child_graph.content_hash != candidate_child_graph_content_hash(node.child_graph):
                    reasons.append(RecursiveProofReasonCode.SOURCE_INTEGRITY_FAILED)
                if node.child_graph.proof_scope.verified_to_timeframe is not None or any(child.candidate_state != "candidate" for child in node.child_graph.children):
                    reasons.append(RecursiveProofReasonCode.CANDIDATE_STATUS_FORBIDDEN)
            for window in (node.native_window, node.child_window):
                if window is not None and (
                    window.content_hash != native_window_content_hash(window)
                    or window.native_rows_hash != native_window_rows_hash(window.candles)
                ):
                    reasons.append(RecursiveProofReasonCode.SOURCE_INTEGRITY_FAILED)
            for attestation in (node.coverage, node.child_coverage):
                if attestation is not None and attestation.content_hash != proof_coverage_attestation_content_hash(attestation):
                    reasons.append(RecursiveProofReasonCode.SOURCE_INTEGRITY_FAILED)
        if self._contains_cycle(bundle.root_node_id, node_by_id):
            reasons.append(RecursiveProofReasonCode.BUNDLE_GRAPH_INVALID)
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _contains_cycle(root_node_id: str, node_by_id: Mapping[str, RecursiveProofNode]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            node = node_by_id.get(node_id)
            if node is None:
                return False
            visiting.add(node_id)
            cycle = any(visit(child_id) for child_id in node.child_node_ids)
            visiting.remove(node_id)
            visited.add(node_id)
            return cycle

        return visit(root_node_id)

    def _verify_node(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        *,
        node_by_id: Mapping[str, RecursiveProofNode],
        root_dataset: DatasetCutoff | None,
        child_results: Any,
    ) -> RecursiveProofNodeResult:
        local_statuses: list[SubdivisionVerificationStatus] = []
        reasons: list[RecursiveProofReasonCode] = []
        warnings: list[str] = []
        local_status, local_reasons = self._validate_node_native_data(bundle, node, root_dataset=root_dataset)
        local_statuses.append(local_status)
        reasons.extend(local_reasons)

        ladder = bundle.policy.timeframe_ladder
        index = ladder.index(node.proof_timeframe)
        is_terminal = index == len(ladder) - 1
        if node.wave.completion_state != "completed":
            local_statuses.append(SubdivisionVerificationStatus.UNPROVEN)
            reasons.append(RecursiveProofReasonCode.PARENT_NOT_COMPLETED)

        if is_terminal:
            if node.child_graph is not None or node.child_node_ids:
                local_statuses.append(SubdivisionVerificationStatus.UNPROVEN)
                reasons.append(RecursiveProofReasonCode.UNEXPECTED_TERMINAL_DESCENDANT)
            status = _aggregate_status(local_statuses)
            if status is SubdivisionVerificationStatus.VERIFIED:
                reasons.append(RecursiveProofReasonCode.TERMINAL_4H_VERIFIED)
            return self._result_for(node, status=status, reasons=tuple(dict.fromkeys(reasons)), child_node_ids=node.child_node_ids, warnings=warnings)

        expected_timeframe = ladder[index + 1]
        graph_status, graph_reasons, matched_child_ids = self._validate_direct_child_graph(
            bundle,
            node,
            expected_timeframe=expected_timeframe,
            node_by_id=node_by_id,
            root_dataset=root_dataset,
        )
        local_statuses.append(graph_status)
        reasons.extend(graph_reasons)

        child_statuses: list[SubdivisionVerificationStatus] = []
        for child_id in matched_child_ids:
            child_result = child_results(child_id)
            child_statuses.append(child_result.status)
            # Bottom-up status propagation must carry the deterministic reason
            # trail as well. Otherwise a monthly result could say only
            # "not_covered" without identifying the missing native 4h proof.
            reasons.extend(child_result.reason_codes)
        if matched_child_ids:
            local_statuses.extend(child_statuses)

        status = _aggregate_status(local_statuses)
        if status is not SubdivisionVerificationStatus.VERIFIED:
            warnings.append("A non-verified recursive result must remain candidate-only in human-facing reports.")
        return self._result_for(node, status=status, reasons=tuple(dict.fromkeys(reasons)), child_node_ids=node.child_node_ids, warnings=warnings)

    def _validate_node_native_data(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        *,
        root_dataset: DatasetCutoff | None,
    ) -> tuple[SubdivisionVerificationStatus, tuple[RecursiveProofReasonCode, ...]]:
        window = node.native_window
        if window is None:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.REQUIRED_NATIVE_WINDOW_MISSING,)
        reasons: list[RecursiveProofReasonCode] = []
        if not window.is_native:
            reasons.append(RecursiveProofReasonCode.REQUIRED_WINDOW_NOT_NATIVE)
        if window.timeframe != node.proof_timeframe:
            reasons.append(RecursiveProofReasonCode.WINDOW_METADATA_INCOMPATIBLE)
        if root_dataset is not None and not dataset_feed_family_matches(root_dataset, window.dataset_cutoff):
            reasons.append(RecursiveProofReasonCode.WINDOW_METADATA_INCOMPATIBLE)
        cutoff = _utc(bundle.analysis_cutoff_utc)
        if _utc(window.dataset_cutoff.cutoff_utc) > cutoff or any(_utc(item.timestamp_utc) > cutoff for item in window.candles):
            reasons.append(RecursiveProofReasonCode.WINDOW_AFTER_CUTOFF)
        if not window.dataset_cutoff.completed_candles_only or any(not item.completed for item in window.candles):
            reasons.append(RecursiveProofReasonCode.INCOMPLETE_CANDLE)
        if len(window.candles) < bundle.policy.minimum_native_bars:
            reasons.append(RecursiveProofReasonCode.COVERAGE_INCOMPLETE)
        coverage = node.coverage
        if coverage is None:
            return SubdivisionVerificationStatus.NOT_COVERED, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.REQUIRED_COVERAGE_ATTESTATION_MISSING)))
        if (
            coverage.proof_timeframe != node.proof_timeframe
            or coverage.dataset_id != window.dataset_cutoff.dataset_id
            or coverage.source_window_hash != window.content_hash
        ):
            reasons.append(RecursiveProofReasonCode.WINDOW_METADATA_INCOMPATIBLE)
        if not coverage.coverage_complete or coverage.missing_interval_ids:
            return SubdivisionVerificationStatus.NOT_COVERED, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.COVERAGE_INCOMPLETE)))
        start = _utc(node.wave.start_pivot.timestamp_utc)
        end = _utc(node.wave.end_pivot.timestamp_utc)
        first = _utc(window.candles[0].timestamp_utc)
        last = _utc(window.candles[-1].timestamp_utc)
        if _utc(coverage.interval_start_utc) > start or _utc(coverage.interval_end_utc) < end or first > start or last < end:
            return SubdivisionVerificationStatus.NOT_COVERED, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.COVERAGE_INTERVAL_INSUFFICIENT)))
        catalog = {item.pivot_id: item for item in node.native_pivot_catalog}
        for pivot in (node.wave.start_pivot, node.wave.end_pivot):
            catalogued = catalog.get(pivot.pivot_id)
            if catalogued is None:
                reasons.append(RecursiveProofReasonCode.PIVOT_NOT_CATALOGUED)
            elif catalogued.content_hash != pivot.content_hash:
                reasons.append(RecursiveProofReasonCode.PIVOT_LINEAGE_MISMATCH)
            elif not _pivot_matches_native_row(pivot, window):
                reasons.append(RecursiveProofReasonCode.PIVOT_VALUE_MISMATCH)
        source_pivot = catalog.get(node.wave.invalidation.source_pivot_id)
        if source_pivot is None:
            reasons.append(RecursiveProofReasonCode.INVALIDATION_PIVOT_UNKNOWN)
        elif _invalidation_breached(node.wave.invalidation, node.wave.start_pivot, window):
            reasons.append(RecursiveProofReasonCode.INVALIDATION_BREACHED)
        if reasons:
            return SubdivisionVerificationStatus.INCONSISTENT, tuple(dict.fromkeys(reasons))
        return SubdivisionVerificationStatus.VERIFIED, ()

    def _validate_direct_child_graph(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        *,
        expected_timeframe: str,
        node_by_id: Mapping[str, RecursiveProofNode],
        root_dataset: DatasetCutoff | None,
    ) -> tuple[SubdivisionVerificationStatus, tuple[RecursiveProofReasonCode, ...], tuple[str, ...]]:
        graph = node.child_graph
        if graph is None:
            # A normal missing graph remains structurally unproven.  A caller
            # may, however, supply an immutable coverage attestation showing
            # that the next required proof window does not exist at all.  That
            # is a data-availability result, not a missing-model-label result.
            known_child_coverage = node.child_coverage
            if known_child_coverage is not None:
                if not known_child_coverage.coverage_complete or known_child_coverage.missing_interval_ids:
                    return (
                        SubdivisionVerificationStatus.NOT_COVERED,
                        (RecursiveProofReasonCode.COVERAGE_INCOMPLETE,),
                        (),
                    )
                if node.child_window is None:
                    return (
                        SubdivisionVerificationStatus.NOT_COVERED,
                        (RecursiveProofReasonCode.REQUIRED_NATIVE_WINDOW_MISSING,),
                        (),
                    )
            return SubdivisionVerificationStatus.UNPROVEN, (RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING,), ()
        if graph.target_timeframe != expected_timeframe:
            return SubdivisionVerificationStatus.UNPROVEN, (RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED,), ()
        if graph.role.value != bundle.role.value or graph.parent_candidate_id != node.wave.wave_id or graph.declared_family != node.wave.declared_family:
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.BUNDLE_GRAPH_INVALID,), ()
        if graph.proof_scope.verified_to_timeframe is not None or any(child.candidate_state != "candidate" for child in graph.children):
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.CANDIDATE_STATUS_FORBIDDEN,), ()

        window = node.child_window
        if window is None:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.REQUIRED_NATIVE_WINDOW_MISSING,), ()
        if not window.is_native:
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.REQUIRED_WINDOW_NOT_NATIVE,), ()
        if window.timeframe != expected_timeframe:
            return SubdivisionVerificationStatus.UNPROVEN, (RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED,), ()
        if root_dataset is not None and not dataset_feed_family_matches(root_dataset, window.dataset_cutoff):
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.WINDOW_METADATA_INCOMPATIBLE,)
        cutoff = _utc(bundle.analysis_cutoff_utc)
        if _utc(window.dataset_cutoff.cutoff_utc) > cutoff or any(_utc(item.timestamp_utc) > cutoff for item in window.candles):
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.WINDOW_AFTER_CUTOFF,)
        if not window.dataset_cutoff.completed_candles_only or any(not item.completed for item in window.candles):
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.INCOMPLETE_CANDLE,)
        child_coverage = node.child_coverage
        if child_coverage is None:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.REQUIRED_COVERAGE_ATTESTATION_MISSING,)
        if (
            child_coverage.proof_timeframe != expected_timeframe
            or child_coverage.dataset_id != window.dataset_cutoff.dataset_id
            or child_coverage.source_window_hash != window.content_hash
        ):
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.WINDOW_METADATA_INCOMPATIBLE,)
        if not child_coverage.coverage_complete or child_coverage.missing_interval_ids:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.COVERAGE_INCOMPLETE,)
        start = _utc(node.wave.start_pivot.timestamp_utc)
        end = _utc(node.wave.end_pivot.timestamp_utc)
        if (
            _utc(child_coverage.interval_start_utc) > start
            or _utc(child_coverage.interval_end_utc) < end
            or _utc(window.candles[0].timestamp_utc) > start
            or _utc(window.candles[-1].timestamp_utc) < end
        ):
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.COVERAGE_INTERVAL_INSUFFICIENT,)
        if len(window.candles) < bundle.policy.minimum_native_bars:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.COVERAGE_INCOMPLETE,)

        reasons: list[RecursiveProofReasonCode] = []
        positions = tuple(child.sequence_position for child in graph.children)
        expected_positions = expected_child_positions(graph.declared_family, positions)
        if expected_positions is None or positions != expected_positions:
            reasons.append(RecursiveProofReasonCode.CHILD_ORDER_INVALID)
        by_position = {child.sequence_position: child for child in graph.children}
        if expected_positions is None or not family_children_are_admissible(graph.declared_family, by_position):
            reasons.append(RecursiveProofReasonCode.FAMILY_CHILDREN_INCOMPATIBLE)
        catalog = {item.pivot_id: item for item in node.child_pivot_catalog}
        for child in graph.children:
            if child.timeframe != expected_timeframe:
                reasons.append(RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED)
            for pivot in (child.start_pivot, child.end_pivot):
                catalogued = catalog.get(pivot.pivot_id)
                if catalogued is None:
                    reasons.append(RecursiveProofReasonCode.PIVOT_NOT_CATALOGUED)
                elif catalogued.content_hash != pivot.content_hash:
                    reasons.append(RecursiveProofReasonCode.PIVOT_LINEAGE_MISMATCH)
                elif not _pivot_matches_native_row(pivot, window):
                    reasons.append(RecursiveProofReasonCode.PIVOT_VALUE_MISMATCH)
            if child.invalidation.source_pivot_id not in catalog:
                reasons.append(RecursiveProofReasonCode.INVALIDATION_PIVOT_UNKNOWN)
            elif _invalidation_breached(child.invalidation, child.start_pivot, window):
                reasons.append(RecursiveProofReasonCode.INVALIDATION_BREACHED)
        if graph.children:
            if node.child_boundary_lineage:
                if not _boundary_lineage_matches(node, graph):
                    reasons.append(RecursiveProofReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH)
            elif not _same_anchor(node.wave.start_pivot, graph.children[0].start_pivot) or not _same_anchor(node.wave.end_pivot, graph.children[-1].end_pivot):
                reasons.append(RecursiveProofReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH)
        for previous, current in zip(graph.children, graph.children[1:]):
            if not _same_anchor(previous.end_pivot, current.start_pivot):
                reasons.append(RecursiveProofReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS)
        if graph.declared_family in MOTIVE_FAMILIES:
            directions = ("up", "down", "up", "down", "up") if node.wave.direction == "up" else ("down", "up", "down", "up", "down")
            if tuple(child.direction for child in graph.children) != directions:
                reasons.append(RecursiveProofReasonCode.DIRECTION_SEQUENCE_INVALID)
            if not _motive_hard_price_rules_pass(graph, node.wave):
                reasons.append(RecursiveProofReasonCode.HARD_PRICE_RULE_FAILURE)
        if graph.declared_family == "diagonal":
            # The existing project does not yet have an approved deterministic
            # diagonal geometry proof pack.  Do not convert allowed overlap
            # into verification merely because the model called it diagonal.
            return SubdivisionVerificationStatus.UNPROVEN, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.DIAGONAL_GEOMETRY_UNPROVEN))), ()

        graph_children = {item.child_id: item for item in graph.children}
        supplied = set(node.child_node_ids)
        required = set(graph_children)
        if supplied != required:
            return SubdivisionVerificationStatus.UNPROVEN, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING))), tuple(sorted(supplied & required))
        for child_id in node.child_node_ids:
            child_node = node_by_id.get(child_id)
            graph_child = graph_children[child_id]
            if child_node is None:
                reasons.append(RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING)
                continue
            if child_node.proof_timeframe != expected_timeframe:
                reasons.append(RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED)
                continue
            if (
                child_node.wave.wave_id != graph_child.child_id
                or child_node.wave.degree != graph_child.degree
                or child_node.wave.declared_family != graph_child.declared_family
                or child_node.wave.direction != graph_child.direction
                or not _same_anchor(child_node.wave.start_pivot, graph_child.start_pivot)
                or not _same_anchor(child_node.wave.end_pivot, graph_child.end_pivot)
            ):
                reasons.append(RecursiveProofReasonCode.CHILD_SNAPSHOT_MISMATCH)
        if reasons:
            status = SubdivisionVerificationStatus.UNPROVEN if all(reason in {RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING, RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED} for reason in reasons) else SubdivisionVerificationStatus.INCONSISTENT
            return status, tuple(dict.fromkeys(reasons)), tuple(node.child_node_ids)
        return SubdivisionVerificationStatus.VERIFIED, (), tuple(node.child_node_ids)

    @staticmethod
    def _result_for(
        node: RecursiveProofNode,
        *,
        status: SubdivisionVerificationStatus,
        reasons: Sequence[RecursiveProofReasonCode],
        child_node_ids: Sequence[str],
        warnings: Sequence[str] = (),
    ) -> RecursiveProofNodeResult:
        unique_reasons = tuple(dict.fromkeys(reasons))
        if status is SubdivisionVerificationStatus.VERIFIED and unique_reasons != (RecursiveProofReasonCode.TERMINAL_4H_VERIFIED,):
            # A non-terminal parent is verified by propagation, not by a model
            # label.  It still gets an empty reason set rather than a terminal
            # leaf claim, so a report can distinguish the proof scope.
            unique_reasons = ()
        return RecursiveProofNodeResult.create(
            node_id=node.node_id,
            proof_timeframe=node.proof_timeframe,
            status=status,
            reason_codes=unique_reasons,
            child_node_ids=tuple(child_node_ids),
            verified_to_timeframe=node.proof_timeframe if status is SubdivisionVerificationStatus.VERIFIED else None,
            warnings=tuple(warnings),
        )


__all__ = [
    "PROOF_BOUNDARY_LINEAGE_SCHEMA_VERSION",
    "PROOF_COVERAGE_ATTESTATION_SCHEMA_VERSION",
    "PROOF_WAVE_SNAPSHOT_SCHEMA_VERSION",
    "RECURSIVE_MULTI_TIMEFRAME_PROOF_CALCULATION_VERSION",
    "RECURSIVE_MULTI_TIMEFRAME_PROOF_POLICY_VERSION",
    "RECURSIVE_MULTI_TIMEFRAME_PROOF_SCHEMA_VERSION",
    "RECURSIVE_PROOF_BUNDLE_SCHEMA_VERSION",
    "RECURSIVE_PROOF_NODE_SCHEMA_VERSION",
    "RECURSIVE_PROOF_RESULT_SCHEMA_VERSION",
    "RECURSIVE_PROOF_TIMEFRAME_LADDER",
    "ProofBoundaryLineage",
    "ProofCoverageAttestation",
    "ProofWaveSnapshot",
    "RecursiveMultiTimeframeProofVerifier",
    "RecursiveProofBundle",
    "RecursiveProofBundlePair",
    "RecursiveProofBundlePairResult",
    "RecursiveProofBundleResult",
    "RecursiveProofHypothesisRole",
    "RecursiveProofNode",
    "RecursiveProofNodeResult",
    "RecursiveProofPolicy",
    "RecursiveProofReasonCode",
    "monthly_to_4h_recursive_proof_policy",
    "proof_boundary_lineage_content_hash",
    "proof_coverage_attestation_content_hash",
    "proof_wave_from_generated_child",
    "proof_wave_from_parent_candidate",
    "proof_wave_snapshot_content_hash",
    "recursive_proof_bundle_content_hash",
    "recursive_proof_bundle_pair_content_hash",
    "recursive_proof_bundle_pair_result_content_hash",
    "recursive_proof_bundle_result_content_hash",
    "recursive_proof_node_content_hash",
    "recursive_proof_node_result_content_hash",
    "recursive_proof_policy_content_hash",
]
