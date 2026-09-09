"""Opt-in, in-memory Elliott shadow analysis for a bounded historical scope.

This module intentionally does not alter the Complete-History shadow runner.
It prepares a separate blind candidate workflow whose Monthly source window
begins at an explicitly selected, catalog-backed pivot.  It reuses the
existing candidate schemas, Stage-1 structural screens, and recursive proof
verifier, but no model candidate can assign a verification status.

The initial public convenience preflight is deliberately narrow:
``NASDAQ:GOOGL`` from the November 2022 Monthly low of ``83.34`` through the
immutable decision-time cutoff already present in a verified full-history
bundle.  The implementation remains provider-free until an explicit caller
supplies both a matching approval and ``allow_model_calls=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .blind_candidate_rules import (
    BlindCandidateRulesPack,
    blind_candidate_rules_pack_content_hash,
    default_blind_candidate_rules_pack,
)
from .complete_history_shadow_runner import (
    COMPLETE_HISTORY_PROOF_LADDER,
    MONTHLY_MAP_OUTPUT_SCHEMA,
    WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
    CompleteHistoryCandidateProvider,
    CompleteHistoryDataset,
    CompleteHistoryMonthlyMap,
    CompleteHistorySegmentProofResult,
    CompleteHistoryShadowError,
    CompleteHistoryShadowReasonCode,
    CompleteHistoryStage1HardRuleCode,
    CompleteHistoryStageRequest,
    CompleteHistoryWeeklyBatchRequest,
    CompleteHistoryWeeklyChildGraphBatch,
    CompleteHistoryShadowStage,
    _build_parent_boundary_lineage,
    _coverage_attestation,
    _focused_window,
    _next_degree,
    _parse_monthly_map,
    _parse_weekly_child_graph_batch,
    _require_provider,
    _source_row_index,
    _validate_monthly_map_pair,
    build_native_pivot_catalog,
    complete_history_bundle_content_hash,
    load_complete_history_bundle,
    screen_complete_history_monthly_map,
    screen_complete_history_stage1_child_graph,
)
from .forecast_records import canonical_sha256
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    NativeCandle,
    NativeOHLCVWindow,
    PivotPriceField,
    TypedPivot,
    WindowCoverageRole,
)
from .lower_timeframe_recursive_proof import (
    ProofCoverageAttestation,
    ProofWaveSnapshot,
    RecursiveMultiTimeframeProofVerifier,
    RecursiveProofBundle,
    RecursiveProofBundlePair,
    RecursiveProofBundlePairResult,
    RecursiveProofHypothesisRole,
    RecursiveProofNode,
    monthly_to_4h_recursive_proof_policy,
)
from .lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus


SCOPED_ELLIOTT_SHADOW_RUNNER_SCHEMA_VERSION = "scoped-elliott-shadow-runner-1.0.0"
SCOPED_ELLIOTT_SHADOW_SCOPE_SCHEMA_VERSION = "scoped-elliott-shadow-scope-1.0.0"
SCOPED_ELLIOTT_SHADOW_POLICY_SCHEMA_VERSION = "scoped-elliott-shadow-policy-1.0.0"
SCOPED_ELLIOTT_SHADOW_PLAN_SCHEMA_VERSION = "scoped-elliott-shadow-plan-1.0.0"
SCOPED_ELLIOTT_SHADOW_APPROVAL_SCHEMA_VERSION = "scoped-elliott-shadow-approval-1.1.0"
SCOPED_ELLIOTT_SHADOW_COVERAGE_SCHEMA_VERSION = "scoped-elliott-shadow-coverage-1.0.0"
SCOPED_ELLIOTT_SHADOW_PREPARATION_SCHEMA_VERSION = "scoped-elliott-shadow-preparation-1.0.0"
SCOPED_ELLIOTT_SHADOW_EXECUTION_SCHEMA_VERSION = "scoped-elliott-shadow-execution-1.0.0"

GOOGL_SCOPED_SYMBOL = "GOOGL"
GOOGL_SCOPED_EXCHANGE = "NASDAQ"
GOOGL_SCOPED_START_TIMESTAMP_UTC = "2022-11-01T00:00:00+00:00"
GOOGL_SCOPED_START_PRICE = 83.34

SCOPED_REQUIRED_PROOF_TIMEFRAMES = ("monthly", "weekly", "daily", "4h")
SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES = ("1h", "15m")
_ROLES = (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_EPSILON = 1e-9
_MAXIMUM_MONTHLY_MAP_SEGMENTS = 12


class ScopedCoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL_OPTIONAL = "partial_optional"
    NOT_COVERED = "not_covered"


class ScopedSegmentPreparationStatus(StrEnum):
    ELIGIBLE = "eligible"
    NOT_COVERED = "not_covered"
    INCONSISTENT = "inconsistent"


class ScopedElliottShadowReasonCode(StrEnum):
    BUNDLE_HASH_INVALID = "bundle_hash_invalid"
    SCOPE_SELECTION_INVALID = "scope_selection_invalid"
    SCOPE_START_PIVOT_NOT_CATALOG_BACKED = "scope_start_pivot_not_catalog_backed"
    SCOPE_START_PIVOT_MISMATCH = "scope_start_pivot_mismatch"
    SCOPE_METADATA_INCOMPATIBLE = "scope_metadata_incompatible"
    PLAN_HASH_INVALID = "plan_hash_invalid"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_HASH_INVALID = "approval_hash_invalid"
    APPROVAL_BINDING_MISMATCH = "approval_binding_mismatch"
    APPROVAL_CALL_LIMIT_MISMATCH = "approval_call_limit_mismatch"
    PROVIDER_BINDING_MISMATCH = "provider_binding_mismatch"
    MODEL_CALL_NOT_AUTHORIZED = "model_call_not_authorized"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    MONTHLY_MAP_INVALID = "monthly_map_invalid"
    MONTHLY_MAP_PAIR_NOT_DISTINCT = "monthly_map_pair_not_distinct"
    MONTHLY_MAP_SCOPE_MISMATCH = "monthly_map_scope_mismatch"
    HYPOTHESIS_BUDGET_EXHAUSTED = "hypothesis_budget_exhausted"


class ScopedElliottShadowError(RuntimeError):
    """Raised before a scoped shadow analysis could make an unsafe transition."""

    def __init__(
        self,
        code: ScopedElliottShadowReasonCode,
        detail: str,
        *,
        attempted_provider_calls: int = 0,
        accepted_provider_calls: int = 0,
        rejected_provider_calls: int = 0,
        reason_codes: Sequence[str] = (),
    ) -> None:
        for name, value in (
            ("attempted_provider_calls", attempted_provider_calls),
            ("accepted_provider_calls", accepted_provider_calls),
            ("rejected_provider_calls", rejected_provider_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer.")
        if accepted_provider_calls + rejected_provider_calls > attempted_provider_calls:
            raise ValueError("Accepted and rejected provider calls cannot exceed attempted provider calls.")
        if isinstance(reason_codes, (str, bytes)) or not isinstance(reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        normalized = tuple(_text(item, field_name="reason_codes") for item in reason_codes)
        if len(normalized) != len(set(normalized)):
            raise ValueError("reason_codes cannot contain duplicates.")
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.attempted_provider_calls = attempted_provider_calls
        self.accepted_provider_calls = accepted_provider_calls
        self.rejected_provider_calls = rejected_provider_calls
        self.reason_codes = normalized


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _utc(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _finite(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite.")
    return float(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Canonical JSON cannot contain non-finite numbers.")
        return value
    raise TypeError(f"Unsupported immutable JSON value: {type(value).__name__}.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


def _contract_hash(value: Any) -> str:
    payload = value.to_dict()
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _coverage_hash(value: "ScopedTimeframeCoverage") -> str:
    return _contract_hash(value)


@dataclass(frozen=True, slots=True)
class ScopedTimeframeCoverage(_JsonContract):
    """Immutable availability fact for one source timeframe inside this scope."""

    timeframe: str
    required_for_proof: bool
    scope_start_utc: str
    scope_end_utc: str
    available_start_utc: str
    available_end_utc: str
    status: ScopedCoverageStatus
    source_dataset_hash: str
    warnings: tuple[str, ...]
    schema_version: str = SCOPED_ELLIOTT_SHADOW_COVERAGE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        timeframe = _text(self.timeframe, field_name="timeframe")
        if timeframe not in (*SCOPED_REQUIRED_PROOF_TIMEFRAMES, *SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES):
            raise ValueError("timeframe is unsupported by the scoped Elliott policy.")
        object.__setattr__(self, "timeframe", timeframe)
        if not isinstance(self.required_for_proof, bool):
            raise TypeError("required_for_proof must be boolean.")
        if self.required_for_proof != (timeframe in SCOPED_REQUIRED_PROOF_TIMEFRAMES):
            raise ValueError("required_for_proof must match the fixed scoped timeframe policy.")
        for name in ("scope_start_utc", "scope_end_utc", "available_start_utc", "available_end_utc"):
            object.__setattr__(self, name, _utc(getattr(self, name), field_name=name))
        if _utc_datetime(self.scope_end_utc) < _utc_datetime(self.scope_start_utc):
            raise ValueError("scope coverage end cannot precede its start.")
        if _utc_datetime(self.available_end_utc) < _utc_datetime(self.available_start_utc):
            raise ValueError("source coverage end cannot precede its start.")
        try:
            status = ScopedCoverageStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status is unsupported.") from exc
        if self.required_for_proof and status is ScopedCoverageStatus.PARTIAL_OPTIONAL:
            raise ValueError("Required proof data cannot be labelled partial_optional.")
        if not self.required_for_proof and status is ScopedCoverageStatus.COMPLETE and self.timeframe in SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES:
            # Complete optional coverage is valid; this branch is intentionally explicit.
            pass
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_dataset_hash", _hash(self.source_dataset_hash, field_name="source_dataset_hash"))
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        warnings = tuple(_text(item, field_name="warnings") for item in self.warnings)
        if len(warnings) != len(set(warnings)):
            raise ValueError("warnings cannot contain duplicates.")
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != _coverage_hash(self):
            raise ValueError("ScopedTimeframeCoverage content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedTimeframeCoverage":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=_coverage_hash(result))


@dataclass(frozen=True, slots=True)
class ScopedElliottShadowScope(_JsonContract):
    """The immutable, catalog-backed boundary of one opt-in shadow analysis."""

    scope_id: str
    symbol: str
    exchange: str
    source_bundle_content_hash: str
    analysis_cutoff_utc: str
    monthly_structural_start_utc: str
    monthly_structural_end_utc: str
    source_selected_start_pivot: TypedPivot
    scoped_selected_start_pivot: TypedPivot
    scoped_monthly_window_hash: str
    monthly_as_of_candle_id: str
    coverage: tuple[ScopedTimeframeCoverage, ...]
    required_proof_timeframes: tuple[str, ...] = SCOPED_REQUIRED_PROOF_TIMEFRAMES
    optional_evidence_timeframes: tuple[str, ...] = SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES
    schema_version: str = SCOPED_ELLIOTT_SHADOW_SCOPE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("scope_id", "symbol", "exchange", "monthly_as_of_candle_id", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(self, "source_bundle_content_hash", _hash(self.source_bundle_content_hash, field_name="source_bundle_content_hash"))
        object.__setattr__(self, "scoped_monthly_window_hash", _hash(self.scoped_monthly_window_hash, field_name="scoped_monthly_window_hash"))
        for name in ("analysis_cutoff_utc", "monthly_structural_start_utc", "monthly_structural_end_utc"):
            object.__setattr__(self, name, _utc(getattr(self, name), field_name=name))
        if _utc_datetime(self.monthly_structural_end_utc) <= _utc_datetime(self.monthly_structural_start_utc):
            raise ValueError("Scoped Monthly structural window must have positive duration.")
        if _utc_datetime(self.analysis_cutoff_utc) < _utc_datetime(self.monthly_structural_end_utc):
            raise ValueError("Decision-time cutoff cannot precede the final completed Monthly structural candle.")
        if not isinstance(self.source_selected_start_pivot, TypedPivot) or not isinstance(self.scoped_selected_start_pivot, TypedPivot):
            raise TypeError("Scoped start pivots must be typed catalog pivots.")
        source = self.source_selected_start_pivot
        scoped = self.scoped_selected_start_pivot
        if source.timeframe != "monthly" or scoped.timeframe != "monthly":
            raise ValueError("Scoped start pivots must be Monthly pivots.")
        if source.timestamp_utc != self.monthly_structural_start_utc or scoped.timestamp_utc != self.monthly_structural_start_utc:
            raise ValueError("Scoped start pivots must equal the Monthly structural start timestamp.")
        if (
            source.price_field is not scoped.price_field
            or abs(source.price - scoped.price) > _EPSILON
            or source.source_bar_hash != scoped.source_bar_hash
        ):
            raise ValueError("Source and sliced-window start pivots must preserve one exact underlying pivot.")
        if scoped.source_window_hash != self.scoped_monthly_window_hash:
            raise ValueError("The scoped start pivot must belong to the exact scoped Monthly source window.")
        if tuple(self.required_proof_timeframes) != SCOPED_REQUIRED_PROOF_TIMEFRAMES:
            raise ValueError("The scoped proof ladder is fixed: monthly -> weekly -> daily -> 4h.")
        if tuple(self.optional_evidence_timeframes) != SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES:
            raise ValueError("The scoped optional evidence policy is fixed: 1h and 15m.")
        if isinstance(self.coverage, (str, bytes)) or not isinstance(self.coverage, Sequence):
            raise TypeError("coverage must be a sequence.")
        coverage = tuple(self.coverage)
        expected = (*SCOPED_REQUIRED_PROOF_TIMEFRAMES, *SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES)
        if tuple(item.timeframe for item in coverage) != expected or not all(isinstance(item, ScopedTimeframeCoverage) for item in coverage):
            raise ValueError("Scope coverage must include every required and optional timeframe in canonical order.")
        if any(item.scope_start_utc != self.monthly_structural_start_utc or item.scope_end_utc != self.monthly_structural_end_utc for item in coverage):
            raise ValueError("Every scope coverage fact must use the exact scoped Monthly structural interval.")
        object.__setattr__(self, "coverage", coverage)
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != scoped_elliott_shadow_scope_content_hash(self):
            raise ValueError("ScopedElliottShadowScope content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedElliottShadowScope":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("scope_id"):
            values["scope_id"] = "scoped_elliott_scope_" + canonical_sha256(
                {
                    "bundle": values.get("source_bundle_content_hash"),
                    "start_pivot": _json_value(values.get("source_selected_start_pivot")),
                    "end": values.get("monthly_structural_end_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=scoped_elliott_shadow_scope_content_hash(result))


def scoped_elliott_shadow_scope_content_hash(value: ScopedElliottShadowScope | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ScopedElliottShadowScope) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ScopedElliottShadowPolicy(_JsonContract):
    """Versioned candidate and proof limits for the bounded Stage-1 workflow."""

    policy_id: str
    proof_timeframe_ladder: tuple[str, ...]
    maximum_children_per_parent: int
    maximum_nodes_per_hypothesis: int
    maximum_model_calls_per_hypothesis: int
    maximum_monthly_map_segments: int
    blind_candidate_rules_pack: BlindCandidateRulesPack = field(default_factory=default_blind_candidate_rules_pack)
    schema_version: str = SCOPED_ELLIOTT_SHADOW_POLICY_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _text(self.policy_id, field_name="policy_id"))
        if tuple(self.proof_timeframe_ladder) != COMPLETE_HISTORY_PROOF_LADDER:
            raise ValueError("The scoped proof ladder is fixed: monthly -> weekly -> daily -> 4h.")
        object.__setattr__(self, "proof_timeframe_ladder", tuple(self.proof_timeframe_ladder))
        for name, minimum in (
            ("maximum_children_per_parent", 3),
            ("maximum_nodes_per_hypothesis", 1),
            ("maximum_model_calls_per_hypothesis", 1),
            ("maximum_monthly_map_segments", 2),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}.")
        if self.maximum_children_per_parent != 5:
            raise ValueError("The initial scoped policy fixes the maximum Elliott child width at five.")
        if self.maximum_monthly_map_segments > _MAXIMUM_MONTHLY_MAP_SEGMENTS:
            raise ValueError("maximum_monthly_map_segments exceeds the strict Monthly-map schema bound.")
        expected_nodes = 1 + (self.maximum_monthly_map_segments - 1) * (1 + self.maximum_children_per_parent)
        if self.maximum_nodes_per_hypothesis != expected_nodes:
            raise ValueError("maximum_nodes_per_hypothesis must match the fixed Stage-1 Monthly-map reservation.")
        if self.maximum_model_calls_per_hypothesis != 2:
            raise ValueError("Stage 1 reserves exactly one Monthly map and one optional Weekly batch per hypothesis.")
        if not isinstance(self.blind_candidate_rules_pack, BlindCandidateRulesPack):
            raise TypeError("blind_candidate_rules_pack must be a BlindCandidateRulesPack.")
        if self.blind_candidate_rules_pack.content_hash != blind_candidate_rules_pack_content_hash(self.blind_candidate_rules_pack):
            raise ValueError("blind_candidate_rules_pack has an invalid canonical hash.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != scoped_elliott_shadow_policy_content_hash(self):
            raise ValueError("ScopedElliottShadowPolicy content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedElliottShadowPolicy":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=scoped_elliott_shadow_policy_content_hash(result))


def scoped_elliott_shadow_policy_content_hash(value: ScopedElliottShadowPolicy | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ScopedElliottShadowPolicy) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def default_scoped_elliott_shadow_policy() -> ScopedElliottShadowPolicy:
    maximum_segments = _MAXIMUM_MONTHLY_MAP_SEGMENTS
    return ScopedElliottShadowPolicy.create(
        policy_id="googl-scoped-elliott-shadow-policy-1.0.0",
        proof_timeframe_ladder=COMPLETE_HISTORY_PROOF_LADDER,
        maximum_children_per_parent=5,
        maximum_nodes_per_hypothesis=1 + (maximum_segments - 1) * 6,
        maximum_model_calls_per_hypothesis=2,
        maximum_monthly_map_segments=maximum_segments,
        blind_candidate_rules_pack=default_blind_candidate_rules_pack(),
    )


@dataclass(frozen=True, slots=True)
class ScopedElliottShadowPlan(_JsonContract):
    """Immutable Stage-1 call reservation bound to the exact scoped pivot."""

    plan_id: str
    bundle_content_hash: str
    scope: ScopedElliottShadowScope
    policy: ScopedElliottShadowPolicy
    roles: tuple[RecursiveProofHypothesisRole, ...]
    total_reserved_model_calls: int
    requires_human_approval: bool
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = SCOPED_ELLIOTT_SHADOW_PLAN_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _text(self.plan_id, field_name="plan_id"))
        object.__setattr__(self, "bundle_content_hash", _hash(self.bundle_content_hash, field_name="bundle_content_hash"))
        if not isinstance(self.scope, ScopedElliottShadowScope) or not isinstance(self.policy, ScopedElliottShadowPolicy):
            raise TypeError("scope and policy must be scoped immutable contracts.")
        if self.scope.source_bundle_content_hash != self.bundle_content_hash:
            raise ValueError("Scoped plan bundle hash must match the scope bundle hash.")
        roles = tuple(RecursiveProofHypothesisRole(item) for item in self.roles)
        if roles != _ROLES:
            raise ValueError("Scoped plans require isolated Primary and Alternative roles in canonical order.")
        object.__setattr__(self, "roles", roles)
        if isinstance(self.total_reserved_model_calls, bool) or not isinstance(self.total_reserved_model_calls, int):
            raise TypeError("total_reserved_model_calls must be an integer.")
        expected_calls = self.policy.maximum_model_calls_per_hypothesis * len(roles)
        if self.total_reserved_model_calls != expected_calls:
            raise ValueError("Scoped Stage-1 plan must reserve exactly four calls across two isolated hypotheses.")
        if not isinstance(self.requires_human_approval, bool) or not self.requires_human_approval:
            raise ValueError("Scoped model calls require explicit human approval.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        warnings = tuple(_text(item, field_name="warnings") for item in self.warnings)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != scoped_elliott_shadow_plan_content_hash(self):
            raise ValueError("ScopedElliottShadowPlan content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedElliottShadowPlan":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("plan_id"):
            values["plan_id"] = "scoped_elliott_plan_" + canonical_sha256(
                {
                    "bundle": values.get("bundle_content_hash"),
                    "scope": _json_value(values.get("scope")),
                    "policy": _json_value(values.get("policy")),
                    "created": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=scoped_elliott_shadow_plan_content_hash(result))


def scoped_elliott_shadow_plan_content_hash(value: ScopedElliottShadowPlan | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ScopedElliottShadowPlan) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ScopedElliottShadowApproval(_JsonContract):
    """Human approval that directly binds bundle, plan, scope, and rules pack."""

    approval_id: str
    plan_content_hash: str
    bundle_content_hash: str
    scope_content_hash: str
    selected_start_pivot_content_hash: str
    rules_pack_content_hash: str
    approved_by: str
    approved_at_utc: str
    exact_call_limit: int
    provider: str
    model: str
    purpose: str = "scoped_elliott_shadow_model_calls"
    schema_version: str = SCOPED_ELLIOTT_SHADOW_APPROVAL_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("approval_id", "approved_by", "provider", "model", "purpose", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if self.purpose != "scoped_elliott_shadow_model_calls":
            raise ValueError("Approval purpose is not authorized for the scoped shadow runner.")
        for name in (
            "plan_content_hash",
            "bundle_content_hash",
            "scope_content_hash",
            "selected_start_pivot_content_hash",
            "rules_pack_content_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "approved_at_utc", _utc(self.approved_at_utc, field_name="approved_at_utc"))
        if isinstance(self.exact_call_limit, bool) or not isinstance(self.exact_call_limit, int) or self.exact_call_limit < 1:
            raise ValueError("exact_call_limit must be a positive integer.")
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != scoped_elliott_shadow_approval_content_hash(self):
            raise ValueError("ScopedElliottShadowApproval content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedElliottShadowApproval":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("approval_id"):
            values["approval_id"] = "scoped_elliott_approval_" + canonical_sha256(
                {
                    "plan": values.get("plan_content_hash"),
                    "scope": values.get("scope_content_hash"),
                    "selected_start_pivot": values.get("selected_start_pivot_content_hash"),
                    "approved_by": values.get("approved_by"),
                    "approved_at": values.get("approved_at_utc"),
                    "provider": values.get("provider"),
                    "model": values.get("model"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=scoped_elliott_shadow_approval_content_hash(result))


def scoped_elliott_shadow_approval_content_hash(value: ScopedElliottShadowApproval | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ScopedElliottShadowApproval) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ScopedSegmentPreparation(_JsonContract):
    """Explicit per-segment lower-timeframe availability before a Weekly call."""

    role: RecursiveProofHypothesisRole
    monthly_map_content_hash: str
    segment_id: str
    status: ScopedSegmentPreparationStatus
    coverage: ProofCoverageAttestation | None
    weekly_request: CompleteHistoryStageRequest | None
    reason_codes: tuple[str, ...]
    schema_version: str = SCOPED_ELLIOTT_SHADOW_PREPARATION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", RecursiveProofHypothesisRole(self.role))
        object.__setattr__(self, "monthly_map_content_hash", _hash(self.monthly_map_content_hash, field_name="monthly_map_content_hash"))
        object.__setattr__(self, "segment_id", _text(self.segment_id, field_name="segment_id"))
        object.__setattr__(self, "status", ScopedSegmentPreparationStatus(self.status))
        if self.coverage is not None and not isinstance(self.coverage, ProofCoverageAttestation):
            raise TypeError("coverage must be a ProofCoverageAttestation or None.")
        if self.weekly_request is not None and not isinstance(self.weekly_request, CompleteHistoryStageRequest):
            raise TypeError("weekly_request must be a CompleteHistoryStageRequest or None.")
        if self.status is ScopedSegmentPreparationStatus.ELIGIBLE:
            if self.coverage is None or not self.coverage.coverage_complete or self.weekly_request is None:
                raise ValueError("Eligible scoped segments require complete coverage and an exact Weekly request.")
        elif self.weekly_request is not None:
            raise ValueError("Unavailable or inconsistent scoped segments cannot carry a Weekly request.")
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        reasons = tuple(_text(item, field_name="reason_codes") for item in self.reason_codes)
        if len(reasons) != len(set(reasons)):
            raise ValueError("reason_codes cannot contain duplicates.")
        if self.status is ScopedSegmentPreparationStatus.ELIGIBLE and reasons:
            raise ValueError("Eligible scoped segments cannot carry failure reasons.")
        if self.status is not ScopedSegmentPreparationStatus.ELIGIBLE and not reasons:
            raise ValueError("Unavailable or inconsistent scoped segments require explicit reasons.")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != scoped_segment_preparation_content_hash(self):
            raise ValueError("ScopedSegmentPreparation content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedSegmentPreparation":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=scoped_segment_preparation_content_hash(result))


def scoped_segment_preparation_content_hash(value: ScopedSegmentPreparation | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ScopedSegmentPreparation) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ScopedElliottShadowExecution(_JsonContract):
    """In-memory only Stage-1 result; every proof status remains deterministic."""

    execution_id: str
    plan_content_hash: str
    scope_content_hash: str
    approval_content_hash: str
    provider: str
    model: str | None
    monthly_maps: tuple[CompleteHistoryMonthlyMap, ...]
    segment_preparations: tuple[ScopedSegmentPreparation, ...]
    weekly_batches: tuple[CompleteHistoryWeeklyChildGraphBatch, ...]
    segment_proof_results: tuple[CompleteHistorySegmentProofResult, ...]
    proof_result: RecursiveProofBundlePairResult
    attempted_provider_calls: int
    accepted_provider_calls: int
    rejected_provider_calls: int
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = SCOPED_ELLIOTT_SHADOW_EXECUTION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", _text(self.execution_id, field_name="execution_id"))
        for name in ("plan_content_hash", "scope_content_hash", "approval_content_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        maps = tuple(self.monthly_maps)
        if tuple(item.role for item in maps) != _ROLES:
            raise ValueError("Scoped execution must preserve one Primary and one Alternative Monthly map.")
        object.__setattr__(self, "monthly_maps", maps)
        preparations = tuple(self.segment_preparations)
        if not all(isinstance(item, ScopedSegmentPreparation) for item in preparations):
            raise TypeError("segment_preparations must contain scoped preparation records.")
        object.__setattr__(self, "segment_preparations", preparations)
        batches = tuple(self.weekly_batches)
        if not all(isinstance(item, CompleteHistoryWeeklyChildGraphBatch) for item in batches):
            raise TypeError("weekly_batches must contain candidate-only Weekly batches.")
        object.__setattr__(self, "weekly_batches", batches)
        proofs = tuple(self.segment_proof_results)
        if not all(isinstance(item, CompleteHistorySegmentProofResult) for item in proofs):
            raise TypeError("segment_proof_results must contain deterministic proof results.")
        object.__setattr__(self, "segment_proof_results", proofs)
        if not isinstance(self.proof_result, RecursiveProofBundlePairResult):
            raise TypeError("proof_result must be a recursive proof bundle pair result.")
        for name in ("attempted_provider_calls", "accepted_provider_calls", "rejected_provider_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer.")
        if self.accepted_provider_calls + self.rejected_provider_calls != self.attempted_provider_calls:
            raise ValueError("Accepted plus rejected provider calls must equal attempted provider calls.")
        if self.attempted_provider_calls > 4:
            raise ValueError("Scoped Stage 1 cannot exceed its four-call reservation.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        object.__setattr__(self, "warnings", tuple(_text(item, field_name="warnings") for item in self.warnings))
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != scoped_elliott_shadow_execution_content_hash(self):
            raise ValueError("ScopedElliottShadowExecution content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ScopedElliottShadowExecution":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("execution_id"):
            values["execution_id"] = "scoped_elliott_execution_" + canonical_sha256(
                {
                    "plan": values.get("plan_content_hash"),
                    "maps": _json_value(values.get("monthly_maps")),
                    "proof": _json_value(values.get("proof_result")),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=scoped_elliott_shadow_execution_content_hash(result))


def scoped_elliott_shadow_execution_content_hash(value: ScopedElliottShadowExecution | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, ScopedElliottShadowExecution) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ScopedElliottShadowPreflight:
    """Read-only preflight material; it contains no provider or database state."""

    bundle: Any
    scope: ScopedElliottShadowScope
    plan: ScopedElliottShadowPlan
    monthly_window: NativeOHLCVWindow
    monthly_pivot_catalog: tuple[TypedPivot, ...]


def _scope_coverage(
    dataset: CompleteHistoryDataset,
    *,
    scope_start_utc: str,
    scope_end_utc: str,
    required_for_proof: bool,
) -> ScopedTimeframeCoverage:
    available_start = dataset.coverage_start_utc
    available_end = dataset.coverage_end_utc
    complete = (
        _utc_datetime(available_start) <= _utc_datetime(scope_start_utc)
        and _utc_datetime(available_end) >= _utc_datetime(scope_end_utc)
    )
    warnings: list[str] = []
    if complete:
        status = ScopedCoverageStatus.COMPLETE
    elif required_for_proof:
        status = ScopedCoverageStatus.NOT_COVERED
        warnings.append("Required proof timeframe does not fully cover the selected scoped structural interval.")
    elif _utc_datetime(available_end) < _utc_datetime(scope_start_utc) or _utc_datetime(available_start) > _utc_datetime(scope_end_utc):
        status = ScopedCoverageStatus.NOT_COVERED
        warnings.append("Optional evidence timeframe has no overlap with the selected scoped structural interval.")
    else:
        status = ScopedCoverageStatus.PARTIAL_OPTIONAL
        warnings.append("Optional evidence timeframe is partial and cannot block deterministic proof.")
    return ScopedTimeframeCoverage.create(
        timeframe=dataset.timeframe,
        required_for_proof=required_for_proof,
        scope_start_utc=scope_start_utc,
        scope_end_utc=scope_end_utc,
        available_start_utc=available_start,
        available_end_utc=available_end,
        status=status,
        source_dataset_hash=dataset.content_hash,
        warnings=tuple(warnings),
    )


def _select_catalog_pivot(
    catalog: Sequence[TypedPivot],
    *,
    timestamp_utc: str,
    price: float,
    price_field: PivotPriceField,
) -> TypedPivot:
    matches = tuple(
        item
        for item in catalog
        if item.timestamp_utc == timestamp_utc
        and item.price_field is price_field
        and abs(item.price - price) <= _EPSILON
    )
    if len(matches) != 1:
        raise ScopedElliottShadowError(
            ScopedElliottShadowReasonCode.SCOPE_START_PIVOT_NOT_CATALOG_BACKED,
            "The requested scoped start pivot is not uniquely present in the immutable Monthly pivot catalog.",
        )
    return matches[0]


def _scoped_monthly_window(
    monthly: CompleteHistoryDataset,
    *,
    source_start_pivot: TypedPivot,
    scope_id: str,
) -> tuple[NativeOHLCVWindow, TypedPivot, tuple[TypedPivot, ...]]:
    start_index = _source_row_index(monthly.source_window, source_start_pivot)
    window = NativeOHLCVWindow.create(
        window_id=f"{scope_id}:monthly:{start_index}:{len(monthly.source_window.candles) - 1}",
        dataset_cutoff=monthly.proof_dataset_cutoff,
        timeframe="monthly",
        candles=monthly.source_window.candles[start_index:],
        coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
        is_native=True,
    )
    catalog = build_native_pivot_catalog(window, pivot_window=1)
    scoped = _select_catalog_pivot(
        catalog,
        timestamp_utc=source_start_pivot.timestamp_utc,
        price=source_start_pivot.price,
        price_field=source_start_pivot.price_field,
    )
    if scoped.source_bar_hash != source_start_pivot.source_bar_hash:
        raise ScopedElliottShadowError(
            ScopedElliottShadowReasonCode.SCOPE_START_PIVOT_MISMATCH,
            "The selected source pivot did not retain its exact immutable source-row lineage in the scoped Monthly window.",
        )
    return window, scoped, catalog


def build_scoped_elliott_shadow_preflight_from_bundle(
    bundle: Any,
    *,
    start_timestamp_utc: str,
    start_price: float,
    start_price_field: PivotPriceField = PivotPriceField.LOW,
    symbol: str = GOOGL_SCOPED_SYMBOL,
    exchange: str = GOOGL_SCOPED_EXCHANGE,
    policy: ScopedElliottShadowPolicy | None = None,
    created_at_utc: str | None = None,
) -> ScopedElliottShadowPreflight:
    """Build a deterministic, provider-free preflight for one selected pivot.

    The function is generic enough for fixtures, while the public runner's
    ``preflight`` convenience method fixes the approved GOOGL selection.
    """

    if bundle.content_hash != complete_history_bundle_content_hash(bundle):
        raise ScopedElliottShadowError(
            ScopedElliottShadowReasonCode.BUNDLE_HASH_INVALID,
            "The immutable complete-history bundle hash is invalid.",
        )
    monthly = bundle.dataset("monthly")
    cutoff = monthly.proof_dataset_cutoff
    if cutoff.resolved_symbol != symbol or cutoff.exchange != exchange:
        raise ScopedElliottShadowError(
            ScopedElliottShadowReasonCode.SCOPE_METADATA_INCOMPATIBLE,
            "The selected scope symbol or exchange does not match the immutable Monthly dataset.",
        )
    start_timestamp_utc = _utc(start_timestamp_utc, field_name="start_timestamp_utc")
    start_price = _finite(start_price, field_name="start_price")
    full_catalog = build_native_pivot_catalog(monthly.source_window, pivot_window=1)
    source_start_pivot = _select_catalog_pivot(
        full_catalog,
        timestamp_utc=start_timestamp_utc,
        price=start_price,
        price_field=PivotPriceField(start_price_field),
    )
    provisional_scope_id = "scoped_elliott_scope_source_" + canonical_sha256(
        {"bundle": bundle.content_hash, "start_pivot": source_start_pivot.to_dict()}
    )[:32]
    scoped_window, scoped_start_pivot, scoped_catalog = _scoped_monthly_window(
        monthly,
        source_start_pivot=source_start_pivot,
        scope_id=provisional_scope_id,
    )
    structural_end = scoped_window.candles[-1].timestamp_utc
    coverage = tuple(
        _scope_coverage(
            bundle.dataset(timeframe),
            scope_start_utc=scoped_start_pivot.timestamp_utc,
            scope_end_utc=structural_end,
            required_for_proof=timeframe in SCOPED_REQUIRED_PROOF_TIMEFRAMES,
        )
        for timeframe in (*SCOPED_REQUIRED_PROOF_TIMEFRAMES, *SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES)
    )
    scope = ScopedElliottShadowScope.create(
        scope_id="",
        symbol=symbol,
        exchange=exchange,
        source_bundle_content_hash=bundle.content_hash,
        analysis_cutoff_utc=bundle.shared_cutoff_utc,
        monthly_structural_start_utc=scoped_start_pivot.timestamp_utc,
        monthly_structural_end_utc=structural_end,
        source_selected_start_pivot=source_start_pivot,
        scoped_selected_start_pivot=scoped_start_pivot,
        scoped_monthly_window_hash=scoped_window.native_rows_hash,
        monthly_as_of_candle_id=scoped_window.candles[-1].candle_id,
        coverage=coverage,
    )
    policy = policy or default_scoped_elliott_shadow_policy()
    if policy.content_hash != scoped_elliott_shadow_policy_content_hash(policy):
        raise ScopedElliottShadowError(
            ScopedElliottShadowReasonCode.PLAN_HASH_INVALID,
            "Scoped policy content hash is invalid.",
        )
    timestamp = _utc(created_at_utc or bundle.shared_cutoff_utc, field_name="created_at_utc")
    plan = ScopedElliottShadowPlan.create(
        plan_id="",
        bundle_content_hash=bundle.content_hash,
        scope=scope,
        policy=policy,
        roles=_ROLES,
        total_reserved_model_calls=4,
        requires_human_approval=True,
        warnings=(
            "Stage 1 reserves exactly four possible calls: isolated Primary and Alternative Monthly maps, then at most one batched Weekly request per role.",
            "The selected Monthly low is catalog-backed and bound directly into the scope, plan, and required approval.",
            "Monthly, Weekly, Daily, and native 4h coverage are required only inside the selected scope; missing lower-timeframe coverage is retained as not_covered rather than blocking candidate analysis.",
            "1h before November 2024 and 15m before February 2026 are optional partial evidence only and can never block proof or create verification.",
            "Blind packets contain only scoped immutable OHLCV, catalog pivots, and the symbol-independent BlindCandidateRulesPack.",
            "Candidate labels remain unproven until RecursiveMultiTimeframeProofVerifier establishes the complete Monthly -> Weekly -> Daily -> native 4h ladder.",
        ),
        created_at_utc=timestamp,
    )
    return ScopedElliottShadowPreflight(
        bundle=bundle,
        scope=scope,
        plan=plan,
        monthly_window=scoped_window,
        monthly_pivot_catalog=scoped_catalog,
    )


def build_googl_scoped_elliott_shadow_preflight(
    bundle_directory: str | Path,
    *,
    created_at_utc: str | None = None,
) -> ScopedElliottShadowPreflight:
    """Read and validate the approved GOOGL November-2022 scoped window."""

    bundle = load_complete_history_bundle(bundle_directory)
    return build_scoped_elliott_shadow_preflight_from_bundle(
        bundle,
        start_timestamp_utc=GOOGL_SCOPED_START_TIMESTAMP_UTC,
        start_price=GOOGL_SCOPED_START_PRICE,
        start_price_field=PivotPriceField.LOW,
        symbol=GOOGL_SCOPED_SYMBOL,
        exchange=GOOGL_SCOPED_EXCHANGE,
        created_at_utc=created_at_utc,
    )


def _monthly_map_packet(
    preflight: ScopedElliottShadowPreflight,
    *,
    role: RecursiveProofHypothesisRole,
) -> Mapping[str, Any]:
    scope = preflight.scope
    plan = preflight.plan
    return MappingProxyType(
        {
            "packet_kind": "blind_scoped_elliott_monthly_map",
            "bundle_content_hash": preflight.bundle.content_hash,
            "scope_content_hash": scope.content_hash,
            "plan_content_hash": plan.content_hash,
            "rules_pack_content_hash": plan.policy.blind_candidate_rules_pack.content_hash,
            "blind_candidate_rules_pack": plan.policy.blind_candidate_rules_pack.to_dict(),
            "role": role.value,
            "analysis_cutoff_utc": scope.analysis_cutoff_utc,
            "proof_ladder": COMPLETE_HISTORY_PROOF_LADDER,
            "monthly_window": preflight.monthly_window.to_dict(),
            "pivot_catalog": [item.to_dict() for item in preflight.monthly_pivot_catalog],
            "as_of_monthly_candle": preflight.monthly_window.candles[-1].to_dict(),
            "approved_monthly_map_scope": {
                "analysis_window_start_utc": scope.monthly_structural_start_utc,
                "analysis_window_end_utc": scope.monthly_structural_end_utc,
                "as_of_observation_cutoff_utc": scope.analysis_cutoff_utc,
                "selected_start_pivot": scope.scoped_selected_start_pivot.to_dict(),
                "selected_source_start_pivot": scope.source_selected_start_pivot.to_dict(),
                "maximum_segments": plan.policy.maximum_monthly_map_segments,
                "origin_control": {"allowed": False, "reason": "explicit scoped start pivot is the structural origin"},
            },
            "coverage": [item.to_dict() for item in scope.coverage],
            "constraints": {
                "blind": True,
                "candidate_only": True,
                "no_alternative_input": True,
                "no_news_or_fundamental_input": True,
                "no_prior_symbol_counts_or_outcomes": True,
                "forbidden_fields": ("verified", "verification_status", "confidence", "report_prose"),
                "rules_pack_content_hash": plan.policy.blind_candidate_rules_pack.content_hash,
            },
        }
    )


def _screen_scoped_monthly_map(
    monthly_map: CompleteHistoryMonthlyMap,
    preflight: ScopedElliottShadowPreflight,
) -> tuple[str, ...]:
    screen = screen_complete_history_monthly_map(
        monthly_map,
        monthly_window=preflight.monthly_window,
        maximum_segments=preflight.plan.policy.maximum_monthly_map_segments,
    )
    reasons = [item.value for item in screen.reason_codes]
    selected = preflight.scope.scoped_selected_start_pivot
    if (
        monthly_map.outer_parent.start_pivot.content_hash != selected.content_hash
        or monthly_map.segments[0].start_pivot.content_hash != selected.content_hash
    ):
        reasons.append(ScopedElliottShadowReasonCode.SCOPE_START_PIVOT_MISMATCH.value)
    if monthly_map.as_of_monthly_candle.candle_id != preflight.scope.monthly_as_of_candle_id:
        reasons.append(ScopedElliottShadowReasonCode.MONTHLY_MAP_SCOPE_MISMATCH.value)
    if monthly_map.analysis_window_start_utc != preflight.scope.monthly_structural_start_utc:
        reasons.append(ScopedElliottShadowReasonCode.MONTHLY_MAP_SCOPE_MISMATCH.value)
    if monthly_map.analysis_window_end_utc != preflight.scope.monthly_structural_end_utc:
        reasons.append(ScopedElliottShadowReasonCode.MONTHLY_MAP_SCOPE_MISMATCH.value)
    return tuple(dict.fromkeys(reasons))


def _weekly_preparations(
    preflight: ScopedElliottShadowPreflight,
    monthly_map: CompleteHistoryMonthlyMap,
    *,
    role: RecursiveProofHypothesisRole,
    created_at_utc: str,
) -> tuple[ScopedSegmentPreparation, ...]:
    weekly = preflight.bundle.dataset("weekly")
    preparations: list[ScopedSegmentPreparation] = []
    for segment in monthly_map.completed_segments:
        target_window, coverage = _focused_window(
            dataset=weekly,
            start_pivot=segment.start_pivot,
            end_pivot=segment.end_pivot,
            window_id_prefix=f"scoped_elliott:{preflight.scope.scope_id}:{role.value}:{segment.wave_id}",
        )
        if target_window is None:
            preparations.append(
                ScopedSegmentPreparation.create(
                    role=role,
                    monthly_map_content_hash=monthly_map.content_hash,
                    segment_id=segment.wave_id,
                    status=ScopedSegmentPreparationStatus.NOT_COVERED,
                    coverage=coverage,
                    weekly_request=None,
                    reason_codes=tuple(coverage.missing_interval_ids),
                )
            )
            continue
        target_catalog = build_native_pivot_catalog(target_window, pivot_window=1)
        try:
            lineage = _build_parent_boundary_lineage(
                parent_wave=segment,
                parent_window=preflight.monthly_window,
                child_window=target_window,
                child_catalog=target_catalog,
                analysis_cutoff_utc=preflight.scope.analysis_cutoff_utc,
            )
        except CompleteHistoryShadowError as exc:
            preparations.append(
                ScopedSegmentPreparation.create(
                    role=role,
                    monthly_map_content_hash=monthly_map.content_hash,
                    segment_id=segment.wave_id,
                    status=ScopedSegmentPreparationStatus.INCONSISTENT,
                    coverage=coverage,
                    weekly_request=None,
                    reason_codes=tuple(exc.reason_codes or (exc.code.value,)),
                )
            )
            continue
        request = CompleteHistoryStageRequest.create(
            request_id="",
            role=role,
            parent_wave=segment,
            stage=CompleteHistoryShadowStage.WEEKLY_CHILDREN,
            target_child_degree=_next_degree(segment.degree),
            target_timeframe="weekly",
            native_window=target_window,
            pivot_catalog=target_catalog,
            parent_boundary_lineage=lineage,
            plan_content_hash=preflight.plan.content_hash,
            policy_content_hash=preflight.plan.policy.content_hash,
            analysis_cutoff_utc=preflight.scope.analysis_cutoff_utc,
            created_at_utc=created_at_utc,
            blind_candidate_rules_pack=preflight.plan.policy.blind_candidate_rules_pack,
        )
        preparations.append(
            ScopedSegmentPreparation.create(
                role=role,
                monthly_map_content_hash=monthly_map.content_hash,
                segment_id=segment.wave_id,
                status=ScopedSegmentPreparationStatus.ELIGIBLE,
                coverage=coverage,
                weekly_request=request,
                reason_codes=(),
            )
        )
    return tuple(preparations)


def _weekly_batch_packet(request: CompleteHistoryWeeklyBatchRequest) -> Mapping[str, Any]:
    """Use the existing exact weekly-batch shape while preserving scope binding."""

    segments: list[dict[str, Any]] = []
    for item in request.segment_requests:
        segments.append(
            {
                "segment_id": item.parent_wave.wave_id,
                "target_timeframe": item.target_timeframe,
                "native_window": item.native_window.to_dict(),
                "pivot_catalog": [pivot.to_dict() for pivot in item.pivot_catalog],
                "boundary_lineage": [
                    {
                        "lineage_id": lineage.lineage_id,
                        "parent_pivot_id": lineage.parent_pivot.pivot_id,
                        "child_pivot_id": lineage.child_pivot.pivot_id,
                        "mapping_kind": lineage.mapping_kind,
                    }
                    for lineage in item.parent_boundary_lineage
                ],
                "parent_wave": item.parent_wave.to_dict(),
            }
        )
    return MappingProxyType(
        {
            "packet_kind": "blind_scoped_elliott_weekly_child_graph_batch",
            "plan_content_hash": request.plan_content_hash,
            "policy_content_hash": request.policy_content_hash,
            "rules_pack_content_hash": request.blind_candidate_rules_pack.content_hash,
            "blind_candidate_rules_pack": request.blind_candidate_rules_pack.to_dict(),
            "role": request.role.value,
            "analysis_cutoff_utc": request.analysis_cutoff_utc,
            "target_timeframe": "weekly",
            "monthly_map_content_hash": request.monthly_map_content_hash,
            "segment_requests": segments,
            "constraints": {
                "blind": True,
                "candidate_only": True,
                "no_alternative_input": True,
                "no_news_or_fundamental_input": True,
                "no_prior_symbol_counts_or_outcomes": True,
                "forbidden_fields": ("verified", "verification_status", "confidence", "report_prose"),
                "rules_pack_content_hash": request.blind_candidate_rules_pack.content_hash,
            },
        }
    )


def _scoped_source_identifiers(preflight: ScopedElliottShadowPreflight) -> tuple[int, int, str, str]:
    """Compatibility-only identifiers for proof contracts; never stored rows."""

    analysis_hash = canonical_sha256(
        {"bundle": preflight.bundle.content_hash, "scope": preflight.scope.content_hash, "kind": "scoped_shadow_analysis_reference"}
    )
    resolution_hash = canonical_sha256(
        {"bundle": preflight.bundle.content_hash, "scope": preflight.scope.content_hash, "kind": "scoped_shadow_resolution_reference"}
    )
    return (
        (int(analysis_hash[:15], 16) % 2_000_000_000) + 1,
        (int(resolution_hash[:15], 16) % 2_000_000_000) + 1,
        analysis_hash,
        resolution_hash,
    )


class ScopedElliottShadowRunner:
    """Manual, opt-in Stage-1 scoped shadow coordinator.

    No constructor reaches a provider, database, market-data endpoint, or
    prior-symbol memory.  ``execute`` needs an explicit provider, a matching
    human approval, and ``allow_model_calls=True``.
    """

    def preflight(
        self,
        bundle_directory: str | Path,
        *,
        created_at_utc: str | None = None,
    ) -> ScopedElliottShadowPreflight:
        return build_googl_scoped_elliott_shadow_preflight(
            bundle_directory,
            created_at_utc=created_at_utc,
        )

    @staticmethod
    def approval_template(
        preflight: ScopedElliottShadowPreflight,
        *,
        approved_by: str,
        approved_at_utc: str,
        provider: str,
        model: str,
    ) -> ScopedElliottShadowApproval:
        """Create a caller-owned approval record; it never invokes a provider."""

        return ScopedElliottShadowApproval.create(
            approval_id="",
            plan_content_hash=preflight.plan.content_hash,
            bundle_content_hash=preflight.bundle.content_hash,
            scope_content_hash=preflight.scope.content_hash,
            selected_start_pivot_content_hash=preflight.scope.scoped_selected_start_pivot.content_hash,
            rules_pack_content_hash=preflight.plan.policy.blind_candidate_rules_pack.content_hash,
            approved_by=approved_by,
            approved_at_utc=approved_at_utc,
            exact_call_limit=preflight.plan.total_reserved_model_calls,
            provider=provider,
            model=model,
        )

    def execute(
        self,
        preflight: ScopedElliottShadowPreflight,
        *,
        provider: CompleteHistoryCandidateProvider,
        approval: ScopedElliottShadowApproval | None,
        allow_model_calls: bool = False,
        created_at_utc: str | None = None,
    ) -> ScopedElliottShadowExecution:
        """Generate candidate-only Monthly maps and optional Weekly batches.

        The method is deliberately in-memory.  It writes no artifact, SQLite
        row, forecast, resolution, or checkpoint, and it stops after the
        Weekly candidate stage regardless of how much data is available.
        """

        if not isinstance(preflight, ScopedElliottShadowPreflight):
            raise TypeError("preflight must be a ScopedElliottShadowPreflight.")
        if preflight.bundle.content_hash != complete_history_bundle_content_hash(preflight.bundle):
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.BUNDLE_HASH_INVALID,
                "The scoped preflight bundle no longer reproduces its canonical hash.",
            )
        if preflight.scope.content_hash != scoped_elliott_shadow_scope_content_hash(preflight.scope):
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.PLAN_HASH_INVALID,
                "The scoped preflight scope no longer reproduces its canonical hash.",
            )
        if preflight.plan.content_hash != scoped_elliott_shadow_plan_content_hash(preflight.plan):
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.PLAN_HASH_INVALID,
                "The scoped preflight plan no longer reproduces its canonical hash.",
            )
        if not allow_model_calls:
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.MODEL_CALL_NOT_AUTHORIZED,
                "allow_model_calls=True is required for every scoped candidate-provider request.",
            )
        if approval is None:
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.APPROVAL_REQUIRED,
                "A separately created scoped human approval is required before any model call.",
            )
        if approval.content_hash != scoped_elliott_shadow_approval_content_hash(approval):
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.APPROVAL_HASH_INVALID,
                "The scoped human approval does not reproduce its canonical hash.",
            )
        if (
            approval.plan_content_hash != preflight.plan.content_hash
            or approval.bundle_content_hash != preflight.bundle.content_hash
            or approval.scope_content_hash != preflight.scope.content_hash
            or approval.selected_start_pivot_content_hash != preflight.scope.scoped_selected_start_pivot.content_hash
            or approval.rules_pack_content_hash != preflight.plan.policy.blind_candidate_rules_pack.content_hash
        ):
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.APPROVAL_BINDING_MISMATCH,
                "The human approval must bind this exact scoped plan, bundle, selected start pivot, and blind rules pack.",
            )
        if approval.exact_call_limit != preflight.plan.total_reserved_model_calls:
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.APPROVAL_CALL_LIMIT_MISMATCH,
                "The scoped approval must authorize exactly the Stage-1 four-call reservation.",
            )
        provider_name, provider_model = _require_provider(provider)
        if provider_name != approval.provider or provider_model != approval.model:
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.PROVIDER_BINDING_MISMATCH,
                "The supplied provider/model does not match the immutable scoped approval.",
            )
        timestamp = _utc(created_at_utc or approval.approved_at_utc, field_name="created_at_utc")
        attempted = {role: 0 for role in _ROLES}
        accepted = {role: 0 for role in _ROLES}
        rejected = {role: 0 for role in _ROLES}
        nodes = {role: 0 for role in _ROLES}

        def reserve_call(role: RecursiveProofHypothesisRole) -> None:
            if attempted[role] >= preflight.plan.policy.maximum_model_calls_per_hypothesis or sum(attempted.values()) >= approval.exact_call_limit:
                raise ScopedElliottShadowError(
                    ScopedElliottShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED,
                    "The bounded scoped Stage-1 provider-call reservation is exhausted.",
                    attempted_provider_calls=sum(attempted.values()),
                    accepted_provider_calls=sum(accepted.values()),
                    rejected_provider_calls=sum(rejected.values()),
                )
            attempted[role] += 1

        def reserve_node(role: RecursiveProofHypothesisRole) -> None:
            if nodes[role] >= preflight.plan.policy.maximum_nodes_per_hypothesis:
                raise ScopedElliottShadowError(
                    ScopedElliottShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED,
                    "The bounded scoped Stage-1 proof-node reservation is exhausted.",
                )
            nodes[role] += 1

        maps_by_role: dict[RecursiveProofHypothesisRole, CompleteHistoryMonthlyMap] = {}
        for role in _ROLES:
            packet = _monthly_map_packet(preflight, role=role)
            reserve_call(role)
            try:
                raw_map = provider.generate_monthly_map(
                    role=role,
                    packet=packet,
                    output_schema=MONTHLY_MAP_OUTPUT_SCHEMA,
                )
                monthly_map = _parse_monthly_map(
                    raw_map,
                    role=role,
                    packet_hash=canonical_sha256(packet),
                    monthly_window=preflight.monthly_window,
                    pivot_catalog=preflight.monthly_pivot_catalog,
                    provider=provider_name,
                    model=provider_model,
                    generated_at_utc=timestamp,
                    maximum_segments=preflight.plan.policy.maximum_monthly_map_segments,
                )
                reasons = _screen_scoped_monthly_map(monthly_map, preflight)
                if reasons:
                    raise ScopedElliottShadowError(
                        ScopedElliottShadowReasonCode.MONTHLY_MAP_SCOPE_MISMATCH,
                        "The Monthly candidate map failed a deterministic scoped structural screen.",
                        reason_codes=reasons,
                    )
            except Exception as exc:
                rejected[role] += 1
                if isinstance(exc, ScopedElliottShadowError):
                    raise ScopedElliottShadowError(
                        exc.code,
                        exc.detail,
                        attempted_provider_calls=sum(attempted.values()),
                        accepted_provider_calls=sum(accepted.values()),
                        rejected_provider_calls=sum(rejected.values()),
                        reason_codes=exc.reason_codes,
                    ) from exc
                raise ScopedElliottShadowError(
                    ScopedElliottShadowReasonCode.MODEL_OUTPUT_INVALID,
                    "The scoped Monthly candidate response could not become valid candidate-only evidence.",
                    attempted_provider_calls=sum(attempted.values()),
                    accepted_provider_calls=sum(accepted.values()),
                    rejected_provider_calls=sum(rejected.values()),
                ) from exc
            accepted[role] += 1
            maps_by_role[role] = monthly_map

        pair_screen = _validate_monthly_map_pair(
            maps_by_role[RecursiveProofHypothesisRole.PRIMARY],
            maps_by_role[RecursiveProofHypothesisRole.ALTERNATIVE],
        )
        if not pair_screen.passed:
            raise ScopedElliottShadowError(
                ScopedElliottShadowReasonCode.MONTHLY_MAP_PAIR_NOT_DISTINCT,
                "Primary and Alternative scoped maps must cover the same selected history while remaining materially different.",
                attempted_provider_calls=sum(attempted.values()),
                accepted_provider_calls=sum(accepted.values()),
                rejected_provider_calls=sum(rejected.values()),
                reason_codes=tuple(item.value for item in pair_screen.reason_codes),
            )

        preparations_by_role = {
            role: _weekly_preparations(preflight, maps_by_role[role], role=role, created_at_utc=timestamp)
            for role in _ROLES
        }
        weekly_batches: dict[RecursiveProofHypothesisRole, CompleteHistoryWeeklyChildGraphBatch] = {}
        weekly_requests: dict[RecursiveProofHypothesisRole, tuple[CompleteHistoryStageRequest, ...]] = {}
        for role in _ROLES:
            requests = tuple(item.weekly_request for item in preparations_by_role[role] if item.weekly_request is not None)
            weekly_requests[role] = requests
            if not requests:
                continue
            request = CompleteHistoryWeeklyBatchRequest.create(
                batch_id="",
                role=role,
                monthly_map_content_hash=maps_by_role[role].content_hash,
                segment_requests=requests,
                plan_content_hash=preflight.plan.content_hash,
                policy_content_hash=preflight.plan.policy.content_hash,
                analysis_cutoff_utc=preflight.scope.analysis_cutoff_utc,
                created_at_utc=timestamp,
                blind_candidate_rules_pack=preflight.plan.policy.blind_candidate_rules_pack,
            )
            reserve_call(role)
            try:
                raw_batch = provider.generate_weekly_child_graph_batch(
                    role=role,
                    packet=_weekly_batch_packet(request),
                    output_schema=WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
                )
                batch = _parse_weekly_child_graph_batch(raw_batch, request=request, provider=provider_name, model=provider_model)
                for graph, segment_request in zip(batch.graphs, request.segment_requests):
                    screen = screen_complete_history_stage1_child_graph(segment_request, graph)
                    if not screen.passed:
                        raise ScopedElliottShadowError(
                            ScopedElliottShadowReasonCode.MODEL_OUTPUT_INVALID,
                            "The Weekly candidate graph failed deterministic Stage-1 structural screening.",
                            reason_codes=tuple(item.value for item in screen.reason_codes),
                        )
            except Exception as exc:
                rejected[role] += 1
                if isinstance(exc, ScopedElliottShadowError):
                    raise ScopedElliottShadowError(
                        exc.code,
                        exc.detail,
                        attempted_provider_calls=sum(attempted.values()),
                        accepted_provider_calls=sum(accepted.values()),
                        rejected_provider_calls=sum(rejected.values()),
                        reason_codes=exc.reason_codes,
                    ) from exc
                raise ScopedElliottShadowError(
                    ScopedElliottShadowReasonCode.MODEL_OUTPUT_INVALID,
                    "The scoped Weekly batch response could not become valid candidate-only evidence.",
                    attempted_provider_calls=sum(attempted.values()),
                    accepted_provider_calls=sum(accepted.values()),
                    rejected_provider_calls=sum(rejected.values()),
                ) from exc
            accepted[role] += 1
            weekly_batches[role] = batch

        analysis_id, resolution_id, analysis_hash, resolution_hash = _scoped_source_identifiers(preflight)
        proof_policy = monthly_to_4h_recursive_proof_policy()
        monthly_dataset = preflight.bundle.dataset("monthly")
        weekly_dataset = preflight.bundle.dataset("weekly")

        def outer_bundle(role: RecursiveProofHypothesisRole) -> RecursiveProofBundle:
            monthly_map = maps_by_role[role]
            reserve_node(role)
            node = RecursiveProofNode.create(
                node_id=monthly_map.outer_parent.wave_id,
                wave=monthly_map.outer_parent,
                proof_timeframe="monthly",
                native_window=preflight.monthly_window,
                native_pivot_catalog=preflight.monthly_pivot_catalog,
                coverage=_coverage_attestation(
                    dataset=monthly_dataset,
                    window=preflight.monthly_window,
                    start_pivot=monthly_map.outer_parent.start_pivot,
                    end_pivot=monthly_map.outer_parent.end_pivot,
                ),
            )
            return RecursiveProofBundle.create(
                bundle_id=f"scoped_elliott_outer:{preflight.scope.scope_id}:{role.value}:{monthly_map.content_hash[:16]}",
                role=role,
                source_analysis_run_id=analysis_id,
                source_analysis_run_hash=analysis_hash,
                source_degree_resolution_id=resolution_id,
                source_degree_resolution_hash=resolution_hash,
                analysis_cutoff_utc=preflight.scope.analysis_cutoff_utc,
                root_node_id=node.node_id,
                nodes=(node,),
                policy=proof_policy,
                shadow_mode=True,
                created_at_utc=timestamp,
            )

        def segment_proof(
            role: RecursiveProofHypothesisRole,
            segment: ProofWaveSnapshot,
            preparation: ScopedSegmentPreparation,
            graph: CandidateChildGraph | None,
        ) -> CompleteHistorySegmentProofResult:
            reserve_node(role)
            root_kwargs: dict[str, Any] = {
                "node_id": segment.wave_id,
                "wave": segment,
                "proof_timeframe": "monthly",
                "native_window": preflight.monthly_window,
                "native_pivot_catalog": preflight.monthly_pivot_catalog,
                "coverage": _coverage_attestation(
                    dataset=monthly_dataset,
                    window=preflight.monthly_window,
                    start_pivot=segment.start_pivot,
                    end_pivot=segment.end_pivot,
                ),
            }
            nodes_for_bundle: list[RecursiveProofNode] = []
            if graph is not None and preparation.weekly_request is not None:
                child_nodes: list[RecursiveProofNode] = []
                for child in graph.children:
                    reserve_node(role)
                    child_wave = ProofWaveSnapshot.create(
                        wave_id=child.child_id,
                        degree=child.degree,
                        declared_family=child.declared_family,
                        direction=child.direction,
                        completion_state="completed",
                        start_pivot=child.start_pivot,
                        end_pivot=child.end_pivot,
                        invalidation=child.invalidation,
                        source_reference_hash=graph.content_hash,
                        source_kind="scoped_elliott_weekly_candidate_child_graph",
                    )
                    child_nodes.append(
                        RecursiveProofNode.create(
                            node_id=child_wave.wave_id,
                            wave=child_wave,
                            proof_timeframe="weekly",
                            native_window=preparation.weekly_request.native_window,
                            native_pivot_catalog=preparation.weekly_request.pivot_catalog,
                            coverage=_coverage_attestation(
                                dataset=weekly_dataset,
                                window=preparation.weekly_request.native_window,
                                start_pivot=child_wave.start_pivot,
                                end_pivot=child_wave.end_pivot,
                            ),
                        )
                    )
                root_kwargs.update(
                    {
                        "child_graph": graph,
                        "child_window": preparation.weekly_request.native_window,
                        "child_pivot_catalog": preparation.weekly_request.pivot_catalog,
                        "child_boundary_lineage": preparation.weekly_request.parent_boundary_lineage,
                        "child_coverage": _coverage_attestation(
                            dataset=weekly_dataset,
                            window=preparation.weekly_request.native_window,
                            start_pivot=segment.start_pivot,
                            end_pivot=segment.end_pivot,
                        ),
                        "child_node_ids": tuple(item.node_id for item in child_nodes),
                    }
                )
                nodes_for_bundle.extend(child_nodes)
            elif preparation.coverage is not None:
                root_kwargs["child_coverage"] = preparation.coverage
            root = RecursiveProofNode.create(**root_kwargs)
            nodes_for_bundle.append(root)
            bundle = RecursiveProofBundle.create(
                bundle_id=f"scoped_elliott_segment:{preflight.scope.scope_id}:{role.value}:{segment.wave_id}",
                role=role,
                source_analysis_run_id=analysis_id,
                source_analysis_run_hash=analysis_hash,
                source_degree_resolution_id=resolution_id,
                source_degree_resolution_hash=resolution_hash,
                analysis_cutoff_utc=preflight.scope.analysis_cutoff_utc,
                root_node_id=root.node_id,
                nodes=tuple(nodes_for_bundle),
                policy=proof_policy,
                shadow_mode=True,
                created_at_utc=timestamp,
            )
            result = RecursiveMultiTimeframeProofVerifier().verify(bundle, shadow_mode=True)
            return CompleteHistorySegmentProofResult.create(
                role=role,
                monthly_map_content_hash=maps_by_role[role].content_hash,
                segment_id=segment.wave_id,
                weekly_batch_content_hash=weekly_batches[role].content_hash if role in weekly_batches and graph is not None else None,
                proof_result=result,
            )

        outer_pair = RecursiveProofBundlePair.create(
            pair_id="",
            primary_bundle=outer_bundle(RecursiveProofHypothesisRole.PRIMARY),
            alternative_bundle=outer_bundle(RecursiveProofHypothesisRole.ALTERNATIVE),
            created_at_utc=timestamp,
        )
        pair_proof = RecursiveMultiTimeframeProofVerifier().verify_pair(outer_pair, shadow_mode=True)
        segment_results: list[CompleteHistorySegmentProofResult] = []
        for role in _ROLES:
            graphs = {}
            if role in weekly_batches:
                graphs = {item.parent_candidate_id: item for item in weekly_batches[role].graphs}
            preparations = {item.segment_id: item for item in preparations_by_role[role]}
            for segment in maps_by_role[role].completed_segments:
                segment_results.append(segment_proof(role, segment, preparations[segment.wave_id], graphs.get(segment.wave_id)))

        warnings = [
            "All verification statuses were produced by RecursiveMultiTimeframeProofVerifier, not the candidate provider.",
            "This scoped runner stops after Weekly candidate generation; Daily and 4h are proof requirements, not Stage-1 model stages.",
            "Missing lower-timeframe intervals were retained as structured not_covered preparations; incompatible exact-price lineage was retained as inconsistent preparation rather than silently repaired.",
            "No SQLite, artifact, forecast, resolution, CLI, market-data, or active-pipeline mutation occurred.",
        ]
        if any(item.status is ScopedSegmentPreparationStatus.NOT_COVERED for values in preparations_by_role.values() for item in values):
            warnings.append("At least one completed Monthly segment is not_covered on Weekly data.")
        if any(item.status is ScopedSegmentPreparationStatus.INCONSISTENT for values in preparations_by_role.values() for item in values):
            warnings.append("At least one completed Monthly segment has incompatible exact-price Monthly-to-Weekly boundary lineage.")
        return ScopedElliottShadowExecution.create(
            execution_id="",
            plan_content_hash=preflight.plan.content_hash,
            scope_content_hash=preflight.scope.content_hash,
            approval_content_hash=approval.content_hash,
            provider=provider_name,
            model=provider_model,
            monthly_maps=tuple(maps_by_role[role] for role in _ROLES),
            segment_preparations=tuple(item for role in _ROLES for item in preparations_by_role[role]),
            weekly_batches=tuple(weekly_batches[role] for role in _ROLES if role in weekly_batches),
            segment_proof_results=tuple(segment_results),
            proof_result=pair_proof,
            attempted_provider_calls=sum(attempted.values()),
            accepted_provider_calls=sum(accepted.values()),
            rejected_provider_calls=sum(rejected.values()),
            warnings=tuple(warnings),
            created_at_utc=timestamp,
        )


__all__ = [
    "GOOGL_SCOPED_EXCHANGE",
    "GOOGL_SCOPED_START_PRICE",
    "GOOGL_SCOPED_START_TIMESTAMP_UTC",
    "GOOGL_SCOPED_SYMBOL",
    "SCOPED_ELLIOTT_SHADOW_APPROVAL_SCHEMA_VERSION",
    "SCOPED_ELLIOTT_SHADOW_EXECUTION_SCHEMA_VERSION",
    "SCOPED_ELLIOTT_SHADOW_PLAN_SCHEMA_VERSION",
    "SCOPED_ELLIOTT_SHADOW_POLICY_SCHEMA_VERSION",
    "SCOPED_ELLIOTT_SHADOW_RUNNER_SCHEMA_VERSION",
    "SCOPED_ELLIOTT_SHADOW_SCOPE_SCHEMA_VERSION",
    "ScopedCoverageStatus",
    "ScopedElliottShadowApproval",
    "ScopedElliottShadowError",
    "ScopedElliottShadowPlan",
    "ScopedElliottShadowPolicy",
    "ScopedElliottShadowPreflight",
    "ScopedElliottShadowReasonCode",
    "ScopedElliottShadowRunner",
    "ScopedElliottShadowScope",
    "ScopedElliottShadowExecution",
    "ScopedSegmentPreparation",
    "ScopedSegmentPreparationStatus",
    "ScopedTimeframeCoverage",
    "build_googl_scoped_elliott_shadow_preflight",
    "build_scoped_elliott_shadow_preflight_from_bundle",
    "default_scoped_elliott_shadow_policy",
    "scoped_elliott_shadow_approval_content_hash",
    "scoped_elliott_shadow_execution_content_hash",
    "scoped_elliott_shadow_plan_content_hash",
    "scoped_elliott_shadow_policy_content_hash",
    "scoped_elliott_shadow_scope_content_hash",
    "scoped_segment_preparation_content_hash",
]
