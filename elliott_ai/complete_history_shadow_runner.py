"""Manual, in-memory complete-history Elliott proof runner.

This module is deliberately a shadow-only coordinator.  It accepts a verified
immutable GOOGL full-history bundle, produces blind Primary and Alternative
candidate trees through a caller-supplied provider, and delegates every proof
status to :mod:`lower_timeframe_recursive_proof`.

It never fetches market data, constructs a live provider, writes SQLite,
creates a forecast, changes an analysis or degree-resolution record, or
integrates with the CLI.  Model calls are disabled unless both a matching human
approval record and an explicit caller flag are supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .blind_candidate_rules import (
    BlindCandidateRulesPack,
    blind_candidate_rules_pack_content_hash,
    default_blind_candidate_rules_pack,
)
from .forecast_records import DatasetCutoff, DatasetHashScope, canonical_sha256
from .market_data_identity import feed_family_from_dataset_cutoff
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGraphRole,
    CandidateInvalidation,
    CandidateProofScope,
    GeneratedChildWave,
    NativeCandle,
    NativeOHLCVWindow,
    PivotPriceField,
    TypedPivot,
    WindowCoverageRole,
    build_native_pivot_catalog,
    candidate_invalidation_openai_json_schema,
    expected_child_positions,
)
from .lower_timeframe_recursive_proof import (
    ProofBoundaryLineage,
    ProofCoverageAttestation,
    ProofWaveSnapshot,
    RecursiveMultiTimeframeProofVerifier,
    RecursiveProofBundle,
    RecursiveProofBundlePair,
    RecursiveProofBundlePairResult,
    RecursiveProofBundleResult,
    RecursiveProofHypothesisRole,
    RecursiveProofNode,
    RecursiveProofPolicy,
    monthly_to_4h_recursive_proof_policy,
)
from .lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus
from .schema import STANDARD_DEGREES


COMPLETE_HISTORY_SHADOW_RUNNER_SCHEMA_VERSION = "complete-history-shadow-runner-1.5.0"
COMPLETE_HISTORY_SHADOW_POLICY_VERSION = "complete-history-shadow-policy-1.4.0"
COMPLETE_HISTORY_BUNDLE_SCHEMA_VERSION = "complete-history-bundle-1.0.0"
COMPLETE_HISTORY_DATASET_SCHEMA_VERSION = "complete-history-dataset-1.0.0"
COMPLETE_HISTORY_PLAN_SCHEMA_VERSION = "complete-history-shadow-plan-1.4.0"
COMPLETE_HISTORY_APPROVAL_SCHEMA_VERSION = "complete-history-shadow-approval-1.5.0"
COMPLETE_HISTORY_ROOT_CANDIDATE_SCHEMA_VERSION = "complete-history-root-candidate-1.1.0"
COMPLETE_HISTORY_MONTHLY_MAP_SCHEMA_VERSION = "complete-history-monthly-map-1.1.0"
COMPLETE_HISTORY_WEEKLY_BATCH_REQUEST_SCHEMA_VERSION = "complete-history-weekly-batch-request-1.1.0"
COMPLETE_HISTORY_WEEKLY_BATCH_SCHEMA_VERSION = "complete-history-weekly-batch-1.1.0"
COMPLETE_HISTORY_SEGMENT_PROOF_RESULT_SCHEMA_VERSION = "complete-history-segment-proof-result-1.1.0"
COMPLETE_HISTORY_STAGE_REQUEST_SCHEMA_VERSION = "complete-history-stage-request-1.4.0"
COMPLETE_HISTORY_EXECUTION_SCHEMA_VERSION = "complete-history-shadow-execution-1.4.0"
COMPLETE_HISTORY_STAGE_ARTIFACT_SCHEMA_VERSION = "complete-history-stage-artifact-1.4.0"

COMPLETE_HISTORY_REQUIRED_TIMEFRAMES = ("monthly", "weekly", "daily", "4h", "1h", "15m")
COMPLETE_HISTORY_PROOF_LADDER = ("monthly", "weekly", "daily", "4h")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_EPSILON = 1e-9
_MAXIMUM_MONTHLY_MAP_SEGMENTS = 12


def _active_monthly_map_node_budget(*, maximum_segments: int, maximum_children: int) -> int:
    """Return the fixed Stage-1 proof-node ceiling for one hypothesis.

    Only completed Monthly segments can receive a Weekly graph.  The terminal
    active segment is retained in the map but is intentionally not a proof
    root, so the maximum is one active outer node plus every possible completed
    segment and its bounded Weekly children.
    """

    return 1 + (maximum_segments - 1) * (1 + maximum_children)


class CompleteHistoryShadowStage(StrEnum):
    MONTHLY_ROOT = "monthly_root"
    WEEKLY_CHILDREN = "weekly_children"
    DAILY_CHILDREN = "daily_children"
    FOUR_HOUR_CHILDREN = "four_hour_children"


class CompleteHistoryMaximumStage(StrEnum):
    """The furthest candidate-generation stage a human approval permits."""

    MONTHLY = "monthly"
    WEEKLY = "weekly"
    DAILY = "daily"
    FOUR_HOUR = "4h"


class CompleteHistoryStageArtifactOutputKind(StrEnum):
    """Structured output kinds; legacy values remain readable but non-resumable."""

    ROOT_CANDIDATE = "root_candidate"
    CHILD_GRAPH = "child_graph"
    MONTHLY_MAP = "monthly_map"
    WEEKLY_CHILD_GRAPH_BATCH = "weekly_child_graph_batch"


_MAXIMUM_STAGE_ORDER = (
    CompleteHistoryMaximumStage.MONTHLY,
    CompleteHistoryMaximumStage.WEEKLY,
    CompleteHistoryMaximumStage.DAILY,
    CompleteHistoryMaximumStage.FOUR_HOUR,
)
_MAXIMUM_STAGE_BY_TIMEFRAME = {
    "monthly": CompleteHistoryMaximumStage.MONTHLY,
    "weekly": CompleteHistoryMaximumStage.WEEKLY,
    "daily": CompleteHistoryMaximumStage.DAILY,
    "4h": CompleteHistoryMaximumStage.FOUR_HOUR,
}


class CompleteHistoryShadowReasonCode(StrEnum):
    MANIFEST_INVALID = "manifest_invalid"
    MANIFEST_HASH_INVALID = "manifest_hash_invalid"
    FILE_HASH_INVALID = "file_hash_invalid"
    CANONICAL_HASH_INVALID = "canonical_hash_invalid"
    REQUIRED_FILE_MISSING = "required_file_missing"
    REQUIRED_TIMEFRAME_MISSING = "required_timeframe_missing"
    DATASET_METADATA_INVALID = "dataset_metadata_invalid"
    DATASET_CUTOFF_INVALID = "dataset_cutoff_invalid"
    DATASET_COVERAGE_INVALID = "dataset_coverage_invalid"
    IDENTITY_BRIDGE_INVALID = "identity_bridge_invalid"
    COVERAGE_MAP_INVALID = "coverage_map_invalid"
    SOURCE_METADATA_INCOMPATIBLE = "source_metadata_incompatible"
    MODEL_CALL_NOT_AUTHORIZED = "model_call_not_authorized"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    APPROVAL_PLAN_MISMATCH = "approval_plan_mismatch"
    APPROVAL_STAGE_SCOPE_MISMATCH = "approval_stage_scope_mismatch"
    APPROVAL_CALL_LIMIT_MISMATCH = "approval_call_limit_mismatch"
    APPROVAL_PROVIDER_MODEL_MISMATCH = "approval_provider_model_mismatch"
    APPROVAL_RULES_PACK_MISMATCH = "approval_rules_pack_mismatch"
    PROVIDER_INTERFACE_INVALID = "provider_interface_invalid"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    STAGE1_SCOPE_INVALID = "stage1_scope_invalid"
    STAGE1_HARD_RULE_FAILED = "stage1_hard_rule_failed"
    ROOT_PAIR_NOT_DISTINCT = "root_pair_not_distinct"
    MONTHLY_MAP_INVALID = "monthly_map_invalid"
    MONTHLY_MAP_PAIR_NOT_DISTINCT = "monthly_map_pair_not_distinct"
    WEEKLY_BATCH_INVALID = "weekly_batch_invalid"
    ACTIVE_MONTHLY_MAP_STAGE_UNSUPPORTED = "active_monthly_map_stage_unsupported"
    PIVOT_LINEAGE_UNAVAILABLE = "pivot_lineage_unavailable"
    HYPOTHESIS_BUDGET_EXHAUSTED = "hypothesis_budget_exhausted"
    TARGET_NOT_COVERED = "target_not_covered"
    ARTIFACT_DIRECTORY_INVALID = "artifact_directory_invalid"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    ARTIFACT_HASH_INVALID = "artifact_hash_invalid"
    ARTIFACT_LINEAGE_CONFLICT = "artifact_lineage_conflict"


class CompleteHistoryStage1HardRuleCode(StrEnum):
    ROOT_WINDOW_NOT_FULL = "root_window_not_full"
    ORIGIN_CONTROL_INVALID = "origin_control_invalid"
    ROOT_PAIR_WINDOW_MISMATCH = "root_pair_window_mismatch"
    ROOT_PAIR_NOT_DISTINCT = "root_pair_not_distinct"
    MONTHLY_MAP_GAP = "monthly_map_gap"
    MONTHLY_MAP_DISCONNECTED = "monthly_map_disconnected"
    MONTHLY_MAP_TERMINAL_ACTIVE_INVALID = "monthly_map_terminal_active_invalid"
    MONTHLY_MAP_AS_OF_INVALID = "monthly_map_as_of_invalid"
    MONTHLY_MAP_PAIR_NOT_DISTINCT = "monthly_map_pair_not_distinct"
    CHILD_SEQUENCE_INCOMPLETE = "child_sequence_incomplete"
    CHILD_BOUNDARY_DISCONNECTED = "child_boundary_disconnected"
    PARENT_CHILD_LINEAGE_MISMATCH = "parent_child_lineage_mismatch"
    STANDARD_IMPULSE_WAVE4_OVERLAP = "standard_impulse_wave4_overlap"


class CompleteHistoryShadowError(RuntimeError):
    """Raised before any unsafe shadow-runner action can happen."""

    def __init__(
        self,
        code: CompleteHistoryShadowReasonCode,
        detail: str,
        *,
        attempted_provider_calls: int = 0,
        accepted_provider_calls: int = 0,
        rejected_provider_calls: int = 0,
        reason_codes: Sequence[str] = (),
    ) -> None:
        for field_name, value in (
            ("attempted_provider_calls", attempted_provider_calls),
            ("accepted_provider_calls", accepted_provider_calls),
            ("rejected_provider_calls", rejected_provider_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{field_name} must be a non-negative integer.")
        if accepted_provider_calls + rejected_provider_calls > attempted_provider_calls:
            raise ValueError("Accepted and rejected provider calls cannot exceed attempted provider calls.")
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.attempted_provider_calls = attempted_provider_calls
        self.accepted_provider_calls = accepted_provider_calls
        self.rejected_provider_calls = rejected_provider_calls
        if isinstance(reason_codes, (str, bytes)) or not isinstance(reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence of non-empty strings.")
        normalized_reason_codes = tuple(_text(item, field_name="reason_codes") for item in reason_codes)
        if len(normalized_reason_codes) != len(set(normalized_reason_codes)):
            raise ValueError("reason_codes cannot contain duplicates.")
        self.reason_codes = normalized_reason_codes


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


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))})
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
    raise TypeError(f"Unsupported immutable JSON value: {type(value).__name__}.")


def _utc(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _int(value: Any, *, field_name: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field_name} must be an integer of at least {minimum}.")
    return value


def _enum(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be one of: {', '.join(item.value for item in enum_type)}.") from exc


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, model_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


def _contract_hash(value: Any) -> str:
    payload = value.to_dict() if hasattr(value, "to_dict") else _json_value(value)
    if isinstance(payload, Mapping):
        payload = dict(payload)
        payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _dataset_cutoff_hash(value: DatasetCutoff) -> str:
    """DatasetCutoff predates content-hash fields; hash its canonical payload."""

    return canonical_sha256(value.to_dict())


def _document_payload_without_canonical_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only the document's own canonical-hash field.

    Nested source-component hashes are evidence and deliberately remain in the
    canonical payload.
    """

    payload = json.loads(json.dumps(value))
    if isinstance(payload.get("canonical_content_sha256"), str):
        payload.pop("canonical_content_sha256", None)
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("canonical_content_sha256"), str):
        metadata = dict(metadata)
        metadata.pop("canonical_content_sha256", None)
        payload["metadata"] = metadata
    return payload


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MANIFEST_INVALID,
            f"Could not read JSON document {path}.",
        ) from exc
    if not isinstance(value, Mapping):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MANIFEST_INVALID,
            f"JSON document {path} must be an object.",
        )
    return value


def _verify_document(
    path: Path,
    *,
    expected_file_hash: str,
    expected_canonical_hash: str | None,
) -> Mapping[str, Any]:
    if not path.is_file():
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.REQUIRED_FILE_MISSING,
            f"Required immutable source file is missing: {path}.",
        )
    actual_file_hash = _file_sha256(path)
    if actual_file_hash != expected_file_hash:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.FILE_HASH_INVALID,
            f"File hash does not match the immutable reference for {path.name}.",
        )
    value = _read_json(path)
    if expected_canonical_hash is not None:
        document_hash = canonical_sha256(_document_payload_without_canonical_hash(value))
        embedded = value.get("canonical_content_sha256")
        if embedded is None and isinstance(value.get("metadata"), Mapping):
            embedded = value["metadata"].get("canonical_content_sha256")
        if embedded != expected_canonical_hash or document_hash != expected_canonical_hash:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID,
                f"Canonical content hash does not match the immutable reference for {path.name}.",
            )
    return value


def _normalised_proof_feed_identity(
    raw_feed_identity: str,
    *,
    timeframe: str,
    provider: str,
    exchange: str,
    provider_symbol: str,
    adjustment: str,
    session: str,
) -> str:
    """Preserve the historical proof label without parsing an old stream ID.

    This compatibility field predates ``MarketDataFeedFamily``.  Its value is
    now generated from already-validated typed metadata.  The raw provider
    stream label is checked against an independently constructed expectation;
    no token is removed from the raw string to manufacture compatibility.
    """

    expected_interval = {
        "monthly": "monthly",
        "weekly": "weekly",
        "daily": "daily",
        "4h": "4h",
        "1h": "1h",
        "15m": "15m",
    }.get(timeframe)
    if expected_interval is None:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.SOURCE_METADATA_INCOMPATIBLE,
            f"feed_identity is not the expected Twelve Data {timeframe} form.",
        )
    expected_stream = "|".join(
        (provider, exchange, provider_symbol, expected_interval, adjustment, session)
    )
    if raw_feed_identity != expected_stream:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.SOURCE_METADATA_INCOMPATIBLE,
            f"feed_identity is not the expected Twelve Data {timeframe} stream.",
        )
    return "|".join((provider, exchange, provider_symbol, adjustment, session))


def _next_degree(degree: str) -> str:
    try:
        index = STANDARD_DEGREES.index(degree)
    except ValueError as exc:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
            f"Unsupported root degree {degree!r}.",
        ) from exc
    if index >= len(STANDARD_DEGREES) - 2:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
            f"Degree {degree!r} cannot support the required three descendant proof levels.",
        )
    return STANDARD_DEGREES[index + 1]


def _terminal_degree(root_degree: str) -> str:
    degree = root_degree
    for _ in range(len(COMPLETE_HISTORY_PROOF_LADDER) - 1):
        degree = _next_degree(degree)
    return degree


@dataclass(frozen=True, slots=True)
class CompleteHistoryDataset(_JsonContract):
    """One verified immutable source dataset used by the proof ladder."""

    timeframe: str
    source_path: str
    source_file_sha256: str
    canonical_content_sha256: str
    raw_feed_identity: str
    proof_dataset_cutoff: DatasetCutoff
    coverage_start_utc: str
    coverage_end_utc: str
    source_window: NativeOHLCVWindow
    schema_version: str = COMPLETE_HISTORY_DATASET_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", _text(self.timeframe, field_name="timeframe"))
        if self.timeframe not in COMPLETE_HISTORY_REQUIRED_TIMEFRAMES:
            raise ValueError("timeframe is not supported by the complete-history runner.")
        for name in ("source_path", "raw_feed_identity", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in ("source_file_sha256", "canonical_content_sha256"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if not isinstance(self.proof_dataset_cutoff, DatasetCutoff):
            raise TypeError("proof_dataset_cutoff must be a DatasetCutoff.")
        if self.proof_dataset_cutoff.timeframe != self.timeframe:
            raise ValueError("Dataset cutoff timeframe must match the source dataset.")
        object.__setattr__(self, "coverage_start_utc", _utc(self.coverage_start_utc, field_name="coverage_start_utc"))
        object.__setattr__(self, "coverage_end_utc", _utc(self.coverage_end_utc, field_name="coverage_end_utc"))
        if _utc_datetime(self.coverage_end_utc) < _utc_datetime(self.coverage_start_utc):
            raise ValueError("Dataset coverage must not end before it begins.")
        if not isinstance(self.source_window, NativeOHLCVWindow):
            raise TypeError("source_window must be a NativeOHLCVWindow.")
        if not self.source_window.is_native or self.source_window.timeframe != self.timeframe:
            raise ValueError("Complete-history data must use a native matching-timeframe window.")
        if _dataset_cutoff_hash(self.source_window.dataset_cutoff) != _dataset_cutoff_hash(self.proof_dataset_cutoff):
            raise ValueError("Source window must use the immutable dataset cutoff.")
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_dataset_content_hash(self):
            raise ValueError("CompleteHistoryDataset content_hash does not match its payload.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "source_path": self.source_path,
            "source_file_sha256": self.source_file_sha256,
            "canonical_content_sha256": self.canonical_content_sha256,
            "raw_feed_identity": self.raw_feed_identity,
            "proof_dataset_cutoff": self.proof_dataset_cutoff.to_dict(),
            "coverage_start_utc": self.coverage_start_utc,
            "coverage_end_utc": self.coverage_end_utc,
            "source_window_hash": self.source_window.content_hash,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryDataset":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=complete_history_dataset_content_hash(result))


def complete_history_dataset_content_hash(value: CompleteHistoryDataset | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryDataset) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryBundle(_JsonContract):
    """Verified GOOGL full-history bundle with immutable source windows."""

    bundle_id: str
    bundle_directory: str
    shared_cutoff_utc: str
    manifest_file_sha256: str
    manifest_canonical_content_sha256: str
    coverage_map_content_sha256: str
    identity_bridge_content_sha256: str
    datasets: tuple[CompleteHistoryDataset, ...]
    warnings: tuple[str, ...]
    schema_version: str = COMPLETE_HISTORY_BUNDLE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("bundle_id", "bundle_directory", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(self, "shared_cutoff_utc", _utc(self.shared_cutoff_utc, field_name="shared_cutoff_utc"))
        for name in ("manifest_file_sha256", "manifest_canonical_content_sha256", "coverage_map_content_sha256", "identity_bridge_content_sha256"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if isinstance(self.datasets, (str, bytes)) or not isinstance(self.datasets, Sequence):
            raise TypeError("datasets must be a sequence.")
        datasets = tuple(self.datasets)
        if not all(isinstance(item, CompleteHistoryDataset) for item in datasets):
            raise TypeError("datasets must contain CompleteHistoryDataset objects.")
        if tuple(item.timeframe for item in datasets) != COMPLETE_HISTORY_REQUIRED_TIMEFRAMES:
            raise ValueError("datasets must provide every required timeframe in canonical order.")
        if any(_utc_datetime(item.proof_dataset_cutoff.cutoff_utc) > _utc_datetime(self.shared_cutoff_utc) for item in datasets):
            raise ValueError("Individual dataset cutoffs cannot exceed the shared cutoff.")
        identities = {
            feed_family_from_dataset_cutoff(item.proof_dataset_cutoff).feed_family_hash
            for item in datasets
        }
        if len(identities) != 1:
            raise ValueError("Proof datasets must derive from one compatible feed family.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        warnings = tuple(_text(item, field_name="warnings") for item in self.warnings)
        object.__setattr__(self, "datasets", datasets)
        object.__setattr__(self, "warnings", warnings)
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_bundle_content_hash(self):
            raise ValueError("CompleteHistoryBundle content_hash does not match its payload.")

    def dataset(self, timeframe: str) -> CompleteHistoryDataset:
        requested = _text(timeframe, field_name="timeframe")
        for dataset in self.datasets:
            if dataset.timeframe == requested:
                return dataset
        raise KeyError(requested)

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryBundle":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=complete_history_bundle_content_hash(result))


def complete_history_bundle_content_hash(value: CompleteHistoryBundle | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryBundle) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryShadowPolicy(_JsonContract):
    """Fixed shadow-policy limits, including the bounded Monthly-map surface."""

    policy_id: str
    proof_timeframe_ladder: tuple[str, ...]
    maximum_children_per_parent: int
    maximum_nodes_per_hypothesis: int
    maximum_model_calls_per_hypothesis: int
    maximum_monthly_map_segments: int = _MAXIMUM_MONTHLY_MAP_SEGMENTS
    blind_candidate_rules_pack: BlindCandidateRulesPack = field(default_factory=default_blind_candidate_rules_pack)
    schema_version: str = COMPLETE_HISTORY_SHADOW_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _text(self.policy_id, field_name="policy_id"))
        schema_version = _text(self.schema_version, field_name="schema_version")
        if isinstance(self.proof_timeframe_ladder, (str, bytes)) or not isinstance(self.proof_timeframe_ladder, Sequence):
            raise TypeError("proof_timeframe_ladder must be a sequence.")
        ladder = tuple(_text(item, field_name="proof_timeframe_ladder") for item in self.proof_timeframe_ladder)
        if ladder != COMPLETE_HISTORY_PROOF_LADDER:
            raise ValueError("The complete-history shadow ladder is fixed: monthly -> weekly -> daily -> 4h.")
        object.__setattr__(self, "proof_timeframe_ladder", ladder)
        object.__setattr__(self, "maximum_children_per_parent", _int(self.maximum_children_per_parent, field_name="maximum_children_per_parent", minimum=3))
        object.__setattr__(
            self,
            "maximum_monthly_map_segments",
            _int(
                self.maximum_monthly_map_segments,
                field_name="maximum_monthly_map_segments",
                minimum=1,
            ),
        )
        if self.maximum_monthly_map_segments > _MAXIMUM_MONTHLY_MAP_SEGMENTS:
            raise ValueError(
                "maximum_monthly_map_segments cannot exceed the versioned strict-output schema limit."
            )
        if schema_version == COMPLETE_HISTORY_SHADOW_POLICY_VERSION:
            expected_nodes = _active_monthly_map_node_budget(
                maximum_segments=self.maximum_monthly_map_segments,
                maximum_children=self.maximum_children_per_parent,
            )
            expected_calls = 2
        else:
            # Historical policies remain readable.  They reserved a full
            # 5-by-5-by-5 tree before the active-parent map contract existed.
            expected_nodes = sum(self.maximum_children_per_parent**depth for depth in range(len(ladder)))
            expected_calls = 1 + sum(self.maximum_children_per_parent**depth for depth in range(len(ladder) - 1))
        object.__setattr__(self, "maximum_nodes_per_hypothesis", _int(self.maximum_nodes_per_hypothesis, field_name="maximum_nodes_per_hypothesis", minimum=1))
        object.__setattr__(self, "maximum_model_calls_per_hypothesis", _int(self.maximum_model_calls_per_hypothesis, field_name="maximum_model_calls_per_hypothesis", minimum=1))
        if self.maximum_children_per_parent != 5:
            raise ValueError("The initial manual complete-history policy reserves the maximum five-child Elliott family width.")
        if self.maximum_nodes_per_hypothesis != expected_nodes:
            raise ValueError("maximum_nodes_per_hypothesis must exactly match the fixed full-tree reservation.")
        if self.maximum_model_calls_per_hypothesis != expected_calls:
            raise ValueError("maximum_model_calls_per_hypothesis must exactly match the versioned shadow-stage call reservation.")
        if not isinstance(self.blind_candidate_rules_pack, BlindCandidateRulesPack):
            raise TypeError("blind_candidate_rules_pack must be a BlindCandidateRulesPack.")
        if self.blind_candidate_rules_pack.content_hash != blind_candidate_rules_pack_content_hash(self.blind_candidate_rules_pack):
            raise ValueError("blind_candidate_rules_pack has an invalid canonical hash.")
        object.__setattr__(self, "schema_version", schema_version)
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_shadow_policy_content_hash(self):
            raise ValueError("CompleteHistoryShadowPolicy content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryShadowPolicy":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=complete_history_shadow_policy_content_hash(result))


def complete_history_shadow_policy() -> CompleteHistoryShadowPolicy:
    return CompleteHistoryShadowPolicy.create(
        policy_id="complete-history-shadow-policy-1.4.0",
        proof_timeframe_ladder=COMPLETE_HISTORY_PROOF_LADDER,
        maximum_children_per_parent=5,
        maximum_nodes_per_hypothesis=_active_monthly_map_node_budget(
            maximum_segments=_MAXIMUM_MONTHLY_MAP_SEGMENTS,
            maximum_children=5,
        ),
        maximum_model_calls_per_hypothesis=2,
        maximum_monthly_map_segments=_MAXIMUM_MONTHLY_MAP_SEGMENTS,
        blind_candidate_rules_pack=default_blind_candidate_rules_pack(),
    )


def complete_history_shadow_policy_content_hash(value: CompleteHistoryShadowPolicy | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryShadowPolicy) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") == "complete-history-shadow-policy-1.1.0":
        payload.pop("blind_candidate_rules_pack", None)
    if payload.get("schema_version") in {
        "complete-history-shadow-policy-1.1.0",
        "complete-history-shadow-policy-1.2.0",
    }:
        payload.pop("maximum_monthly_map_segments", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryStagePlan(_JsonContract):
    stage: CompleteHistoryShadowStage
    target_timeframe: str
    parent_slots_per_hypothesis: int
    reserved_model_calls_per_hypothesis: int
    coverage_behavior: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", _enum(self.stage, CompleteHistoryShadowStage, field_name="stage"))
        object.__setattr__(self, "target_timeframe", _text(self.target_timeframe, field_name="target_timeframe"))
        object.__setattr__(self, "parent_slots_per_hypothesis", _int(self.parent_slots_per_hypothesis, field_name="parent_slots_per_hypothesis", minimum=1))
        object.__setattr__(self, "reserved_model_calls_per_hypothesis", _int(self.reserved_model_calls_per_hypothesis, field_name="reserved_model_calls_per_hypothesis", minimum=1))
        object.__setattr__(self, "coverage_behavior", _text(self.coverage_behavior, field_name="coverage_behavior"))


@dataclass(frozen=True, slots=True)
class CompleteHistoryHypothesisPlan(_JsonContract):
    role: RecursiveProofHypothesisRole
    maximum_nodes: int
    reserved_model_calls: int
    stages: tuple[CompleteHistoryStagePlan, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        object.__setattr__(self, "maximum_nodes", _int(self.maximum_nodes, field_name="maximum_nodes", minimum=1))
        object.__setattr__(self, "reserved_model_calls", _int(self.reserved_model_calls, field_name="reserved_model_calls", minimum=1))
        if isinstance(self.stages, (str, bytes)) or not isinstance(self.stages, Sequence):
            raise TypeError("stages must be a sequence.")
        stages = tuple(self.stages)
        if not all(isinstance(item, CompleteHistoryStagePlan) for item in stages):
            raise TypeError("stages must contain CompleteHistoryStagePlan objects.")
        stage_sequence = tuple(item.stage for item in stages)
        supported_sequences = {
            (
                CompleteHistoryShadowStage.MONTHLY_ROOT,
                CompleteHistoryShadowStage.WEEKLY_CHILDREN,
            ),
            # Historical plan contracts remain readable but cannot resume into
            # the active-parent Monthly-map workflow.
            (
                CompleteHistoryShadowStage.MONTHLY_ROOT,
                CompleteHistoryShadowStage.WEEKLY_CHILDREN,
                CompleteHistoryShadowStage.DAILY_CHILDREN,
                CompleteHistoryShadowStage.FOUR_HOUR_CHILDREN,
            ),
        }
        if stage_sequence not in supported_sequences:
            raise ValueError("Complete-history stages must preserve either the active Monthly-map pair or the historical root/weekly/daily/4h order.")
        if sum(item.reserved_model_calls_per_hypothesis for item in stages) != self.reserved_model_calls:
            raise ValueError("Reserved stage calls must equal reserved_model_calls.")
        object.__setattr__(self, "stages", stages)


@dataclass(frozen=True, slots=True)
class CompleteHistoryShadowPlan(_JsonContract):
    plan_id: str
    bundle_content_hash: str
    policy: CompleteHistoryShadowPolicy
    hypotheses: tuple[CompleteHistoryHypothesisPlan, ...]
    total_reserved_model_calls: int
    requires_human_approval: bool
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = COMPLETE_HISTORY_PLAN_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _text(self.plan_id, field_name="plan_id"))
        schema_version = _text(self.schema_version, field_name="schema_version")
        object.__setattr__(self, "bundle_content_hash", _hash(self.bundle_content_hash, field_name="bundle_content_hash"))
        if not isinstance(self.policy, CompleteHistoryShadowPolicy):
            raise TypeError("policy must be a CompleteHistoryShadowPolicy.")
        if isinstance(self.hypotheses, (str, bytes)) or not isinstance(self.hypotheses, Sequence):
            raise TypeError("hypotheses must be a sequence.")
        hypotheses = tuple(self.hypotheses)
        if tuple(item.role for item in hypotheses) != (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE):
            raise ValueError("A plan must retain one isolated Primary and one isolated Alternative hypothesis.")
        if any(not isinstance(item, CompleteHistoryHypothesisPlan) for item in hypotheses):
            raise TypeError("hypotheses must contain CompleteHistoryHypothesisPlan objects.")
        if any(item.maximum_nodes != self.policy.maximum_nodes_per_hypothesis for item in hypotheses):
            raise ValueError("Hypothesis node budgets must match the policy.")
        if any(item.reserved_model_calls != self.policy.maximum_model_calls_per_hypothesis for item in hypotheses):
            raise ValueError("Hypothesis call budgets must match the policy.")
        object.__setattr__(self, "total_reserved_model_calls", _int(self.total_reserved_model_calls, field_name="total_reserved_model_calls", minimum=1))
        if self.total_reserved_model_calls != sum(item.reserved_model_calls for item in hypotheses):
            raise ValueError("total_reserved_model_calls must equal the two independent hypothesis budgets.")
        if not isinstance(self.requires_human_approval, bool) or not self.requires_human_approval:
            raise ValueError("Complete-history model calls must require explicit human approval.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        object.__setattr__(self, "warnings", tuple(_text(item, field_name="warnings") for item in self.warnings))
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        if schema_version == COMPLETE_HISTORY_PLAN_SCHEMA_VERSION and any(
            tuple(stage.stage for stage in item.stages)
            != (
                CompleteHistoryShadowStage.MONTHLY_ROOT,
                CompleteHistoryShadowStage.WEEKLY_CHILDREN,
            )
            for item in hypotheses
        ):
            raise ValueError("The active-parent Monthly-map plan can reserve only Monthly and batched Weekly stages.")
        object.__setattr__(self, "schema_version", schema_version)
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_shadow_plan_content_hash(self):
            raise ValueError("CompleteHistoryShadowPlan content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryShadowPlan":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("plan_id"):
            values["plan_id"] = "complete_history_plan_" + canonical_sha256(
                {
                    "bundle": values.get("bundle_content_hash"),
                    "policy": _json_value(values.get("policy")),
                    "created_at_utc": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_shadow_plan_content_hash(result))


def complete_history_shadow_plan_content_hash(value: CompleteHistoryShadowPlan | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryShadowPlan) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") == "complete-history-shadow-plan-1.1.0":
        policy = payload.get("policy")
        if isinstance(policy, Mapping):
            legacy_policy = dict(policy)
            legacy_policy.pop("blind_candidate_rules_pack", None)
            payload["policy"] = legacy_policy
    return canonical_sha256(payload)


def _maximum_stage_index(value: CompleteHistoryMaximumStage) -> int:
    return _MAXIMUM_STAGE_ORDER.index(value)


def _stage_is_authorized(
    *,
    target_timeframe: str,
    max_stage: CompleteHistoryMaximumStage,
) -> bool:
    target = _MAXIMUM_STAGE_BY_TIMEFRAME.get(target_timeframe)
    if target is None:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
            f"The proof ladder cannot authorize unsupported timeframe {target_timeframe!r}.",
        )
    return _maximum_stage_index(target) <= _maximum_stage_index(max_stage)


def complete_history_stage_call_limit(
    plan: CompleteHistoryShadowPlan,
    max_stage: CompleteHistoryMaximumStage | str,
) -> int:
    """Return the deterministic total call ceiling through one approved stage.

    The result covers both independent hypotheses.  It is intentionally a
    transparent stage reservation, not a model estimate or adaptive budget.
    """

    if not isinstance(plan, CompleteHistoryShadowPlan):
        raise TypeError("plan must be a CompleteHistoryShadowPlan.")
    if plan.content_hash != complete_history_shadow_plan_content_hash(plan):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID,
            "Complete-history plan hash is invalid.",
    )
    stage = _enum(max_stage, CompleteHistoryMaximumStage, field_name="max_stage")
    if (
        plan.schema_version == COMPLETE_HISTORY_PLAN_SCHEMA_VERSION
        and _maximum_stage_index(stage) > _maximum_stage_index(CompleteHistoryMaximumStage.WEEKLY)
    ):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.ACTIVE_MONTHLY_MAP_STAGE_UNSUPPORTED,
            "The active-parent Monthly-map plan deliberately ends at the batched Weekly candidate stage.",
        )
    allowed_index = _maximum_stage_index(stage)
    total = 0
    for hypothesis in plan.hypotheses:
        for item in hypothesis.stages:
            item_stage = _MAXIMUM_STAGE_BY_TIMEFRAME.get(item.target_timeframe)
            if item_stage is None:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID,
                    "Plan contains an unsupported stage timeframe.",
                )
            if _maximum_stage_index(item_stage) <= allowed_index:
                total += item.reserved_model_calls_per_hypothesis
    return total


@dataclass(frozen=True, slots=True)
class CompleteHistoryHumanApproval(_JsonContract):
    approval_id: str
    plan_content_hash: str
    approved_by: str
    approved_at_utc: str
    max_stage: CompleteHistoryMaximumStage = CompleteHistoryMaximumStage.WEEKLY
    exact_call_limit: int = 4
    provider: str = "openai"
    model: str = "gpt-5.6-terra"
    rules_pack_content_hash: str = field(
        default_factory=lambda: default_blind_candidate_rules_pack().content_hash
    )
    purpose: str = "complete_history_shadow_model_calls"
    schema_version: str = COMPLETE_HISTORY_APPROVAL_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("approval_id", "approved_by", "provider", "model", "purpose", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if self.purpose != "complete_history_shadow_model_calls":
            raise ValueError("Approval purpose is not authorized for the complete-history shadow runner.")
        object.__setattr__(self, "plan_content_hash", _hash(self.plan_content_hash, field_name="plan_content_hash"))
        object.__setattr__(
            self,
            "rules_pack_content_hash",
            _hash(self.rules_pack_content_hash, field_name="rules_pack_content_hash"),
        )
        object.__setattr__(self, "approved_at_utc", _utc(self.approved_at_utc, field_name="approved_at_utc"))
        object.__setattr__(self, "max_stage", _enum(self.max_stage, CompleteHistoryMaximumStage, field_name="max_stage"))
        object.__setattr__(self, "exact_call_limit", _int(self.exact_call_limit, field_name="exact_call_limit", minimum=1))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_human_approval_content_hash(self):
            raise ValueError("CompleteHistoryHumanApproval content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryHumanApproval":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("approval_id"):
            values["approval_id"] = "complete_history_approval_" + canonical_sha256(
                {
                    "plan": values.get("plan_content_hash"),
                    "approved_by": values.get("approved_by"),
                    "approved_at_utc": values.get("approved_at_utc"),
                    "max_stage": _json_value(values.get("max_stage", CompleteHistoryMaximumStage.WEEKLY)),
                    "exact_call_limit": values.get("exact_call_limit", 4),
                    "provider": values.get("provider", "openai"),
                    "model": values.get("model", "gpt-5.6-terra"),
                    "rules_pack_content_hash": values.get(
                        "rules_pack_content_hash",
                        default_blind_candidate_rules_pack().content_hash,
                    ),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_human_approval_content_hash(result))


def complete_history_human_approval_content_hash(value: CompleteHistoryHumanApproval | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryHumanApproval) else dict(value)
    payload.pop("content_hash", None)
    # Version 1.1.0 approvals were already immutable before provider/model
    # binding existed. Keep their stored hashes readable without pretending the
    # legacy record had the new binding fields.
    if payload.get("schema_version") in {
        "complete-history-shadow-approval-1.1.0",
        "complete-history-shadow-approval-1.2.0",
    }:
        payload.pop("rules_pack_content_hash", None)
    if payload.get("schema_version") == "complete-history-shadow-approval-1.1.0":
        payload.pop("provider", None)
        payload.pop("model", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryRootCandidate(_JsonContract):
    candidate_id: str
    role: RecursiveProofHypothesisRole
    wave: ProofWaveSnapshot
    analysis_window_start_utc: str
    analysis_window_end_utc: str
    origin_control_start_pivot: TypedPivot | None
    origin_control_end_pivot: TypedPivot | None
    source_packet_hash: str
    provider: str
    model: str | None
    generated_at_utc: str
    candidate_state: str = "candidate"
    schema_version: str = COMPLETE_HISTORY_ROOT_CANDIDATE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, field_name="candidate_id"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        if not isinstance(self.wave, ProofWaveSnapshot):
            raise TypeError("wave must be a ProofWaveSnapshot.")
        object.__setattr__(self, "analysis_window_start_utc", _utc(self.analysis_window_start_utc, field_name="analysis_window_start_utc"))
        object.__setattr__(self, "analysis_window_end_utc", _utc(self.analysis_window_end_utc, field_name="analysis_window_end_utc"))
        if _utc_datetime(self.analysis_window_end_utc) <= _utc_datetime(self.analysis_window_start_utc):
            raise ValueError("analysis_window_end_utc must be after analysis_window_start_utc.")
        if self.origin_control_start_pivot is not None and not isinstance(self.origin_control_start_pivot, TypedPivot):
            raise TypeError("origin_control_start_pivot must be a TypedPivot or None.")
        if self.origin_control_end_pivot is not None and not isinstance(self.origin_control_end_pivot, TypedPivot):
            raise TypeError("origin_control_end_pivot must be a TypedPivot or None.")
        if (self.origin_control_start_pivot is None) != (self.origin_control_end_pivot is None):
            raise ValueError("Origin control requires both start and end typed pivots.")
        if self.wave.end_pivot.timestamp_utc != self.analysis_window_end_utc:
            raise ValueError("A complete-history root must end at the approved monthly analysis-window boundary.")
        if self.origin_control_start_pivot is None:
            if self.wave.start_pivot.timestamp_utc != self.analysis_window_start_utc:
                raise ValueError("A root without origin control must start at the approved monthly analysis-window boundary.")
        else:
            if (
                self.origin_control_start_pivot.timestamp_utc != self.analysis_window_start_utc
                or self.origin_control_end_pivot.content_hash != self.wave.start_pivot.content_hash
                or _utc_datetime(self.origin_control_end_pivot.timestamp_utc) <= _utc_datetime(self.origin_control_start_pivot.timestamp_utc)
            ):
                raise ValueError("Origin control must connect the approved IPO boundary to the root start pivot.")
        object.__setattr__(self, "source_packet_hash", _hash(self.source_packet_hash, field_name="source_packet_hash"))
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        object.__setattr__(self, "generated_at_utc", _utc(self.generated_at_utc, field_name="generated_at_utc"))
        if self.candidate_state != "candidate":
            raise ValueError("Root candidates must remain candidate-only.")
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_root_candidate_content_hash(self):
            raise ValueError("CompleteHistoryRootCandidate content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryRootCandidate":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("candidate_id"):
            values["candidate_id"] = "monthly_root_candidate_" + canonical_sha256(
                {
                    "role": _json_value(values.get("role")),
                    "wave": _json_value(values.get("wave")),
                    "analysis_window_start_utc": values.get("analysis_window_start_utc"),
                    "analysis_window_end_utc": values.get("analysis_window_end_utc"),
                    "origin_control_start_pivot": _json_value(values.get("origin_control_start_pivot")),
                    "origin_control_end_pivot": _json_value(values.get("origin_control_end_pivot")),
                    "packet": values.get("source_packet_hash"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_root_candidate_content_hash(result))


def complete_history_root_candidate_content_hash(value: CompleteHistoryRootCandidate | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryRootCandidate) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") == "complete-history-root-candidate-1.0.0":
        payload.pop("analysis_window_start_utc", None)
        payload.pop("analysis_window_end_utc", None)
        payload.pop("origin_control_start_pivot", None)
        payload.pop("origin_control_end_pivot", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryMonthlyMap(_JsonContract):
    """A full-history Monthly structural map with one terminal active segment.

    The map is deliberately not a verification result.  Its active outer parent
    can contain completed top-level Monthly segments.  Those completed segments
    are separately eligible for a lower-timeframe candidate request; the final
    active segment only carries the map through the immutable as-of candle.
    """

    map_id: str
    role: RecursiveProofHypothesisRole
    outer_parent: ProofWaveSnapshot
    segments: tuple[ProofWaveSnapshot, ...]
    as_of_monthly_candle: NativeCandle
    analysis_window_start_utc: str
    analysis_window_end_utc: str
    origin_control_start_pivot: TypedPivot | None
    origin_control_end_pivot: TypedPivot | None
    source_packet_hash: str
    provider: str
    model: str | None
    generated_at_utc: str
    candidate_state: str = "candidate"
    schema_version: str = COMPLETE_HISTORY_MONTHLY_MAP_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "map_id", _text(self.map_id, field_name="map_id"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        if not isinstance(self.outer_parent, ProofWaveSnapshot):
            raise TypeError("outer_parent must be a ProofWaveSnapshot.")
        if self.outer_parent.completion_state != "active":
            raise ValueError("A complete-history Monthly map requires an active outer parent.")
        if isinstance(self.segments, (str, bytes)) or not isinstance(self.segments, Sequence):
            raise TypeError("segments must be a sequence.")
        segments = tuple(self.segments)
        if not segments or len(segments) > _MAXIMUM_MONTHLY_MAP_SEGMENTS:
            raise ValueError("A Monthly map must contain a bounded non-empty segment sequence.")
        if not all(isinstance(item, ProofWaveSnapshot) for item in segments):
            raise TypeError("segments must contain ProofWaveSnapshot objects.")
        if len({item.wave_id for item in segments}) != len(segments):
            raise ValueError("Monthly map segment IDs must be unique.")
        if any(item.completion_state != "completed" for item in segments[:-1]) or segments[-1].completion_state != "active":
            raise ValueError("Only the final Monthly map segment may be active.")
        if self.outer_parent.start_pivot.content_hash != segments[0].start_pivot.content_hash:
            raise ValueError("The outer parent must start at the first Monthly map segment boundary.")
        if self.outer_parent.end_pivot.content_hash != segments[-1].end_pivot.content_hash:
            raise ValueError("The outer parent last structural pivot must match the active final segment.")
        if any(
            previous.end_pivot.content_hash != current.start_pivot.content_hash
            for previous, current in zip(segments, segments[1:])
        ):
            raise ValueError("Monthly map segments must connect through shared typed pivots.")
        source_window_hash = self.outer_parent.start_pivot.source_window_hash
        if any(
            pivot.source_window_hash != source_window_hash
            for wave in (self.outer_parent, *segments)
            for pivot in (wave.start_pivot, wave.end_pivot)
        ):
            raise ValueError("Monthly map pivots must retain one immutable Monthly source-window lineage.")
        object.__setattr__(self, "segments", segments)
        if not isinstance(self.as_of_monthly_candle, NativeCandle):
            raise TypeError("as_of_monthly_candle must be a NativeCandle.")
        object.__setattr__(self, "analysis_window_start_utc", _utc(self.analysis_window_start_utc, field_name="analysis_window_start_utc"))
        object.__setattr__(self, "analysis_window_end_utc", _utc(self.analysis_window_end_utc, field_name="analysis_window_end_utc"))
        if _utc_datetime(self.analysis_window_end_utc) <= _utc_datetime(self.analysis_window_start_utc):
            raise ValueError("analysis_window_end_utc must be after analysis_window_start_utc.")
        if self.as_of_monthly_candle.timestamp_utc != self.analysis_window_end_utc:
            raise ValueError("The Monthly as-of candle must be the final completed Monthly source candle.")
        if self.origin_control_start_pivot is not None and not isinstance(self.origin_control_start_pivot, TypedPivot):
            raise TypeError("origin_control_start_pivot must be a TypedPivot or None.")
        if self.origin_control_end_pivot is not None and not isinstance(self.origin_control_end_pivot, TypedPivot):
            raise TypeError("origin_control_end_pivot must be a TypedPivot or None.")
        if (self.origin_control_start_pivot is None) != (self.origin_control_end_pivot is None):
            raise ValueError("Origin control requires both start and end typed pivots.")
        map_start = segments[0].start_pivot
        if self.origin_control_start_pivot is None:
            if map_start.timestamp_utc != self.analysis_window_start_utc:
                raise ValueError("A Monthly map without origin control must begin at the approved IPO boundary.")
        elif (
            self.origin_control_start_pivot.timestamp_utc != self.analysis_window_start_utc
            or self.origin_control_end_pivot.content_hash != map_start.content_hash
            or _utc_datetime(self.origin_control_end_pivot.timestamp_utc)
            <= _utc_datetime(self.origin_control_start_pivot.timestamp_utc)
        ):
            raise ValueError("Origin control must connect the IPO boundary directly to the first map segment.")
        object.__setattr__(self, "source_packet_hash", _hash(self.source_packet_hash, field_name="source_packet_hash"))
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        object.__setattr__(self, "generated_at_utc", _utc(self.generated_at_utc, field_name="generated_at_utc"))
        if self.candidate_state != "candidate":
            raise ValueError("Monthly maps must remain candidate-only.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_monthly_map_content_hash(self):
            raise ValueError("CompleteHistoryMonthlyMap content_hash does not match its payload.")

    @property
    def completed_segments(self) -> tuple[ProofWaveSnapshot, ...]:
        return tuple(item for item in self.segments if item.completion_state == "completed")

    @property
    def active_final_segment(self) -> ProofWaveSnapshot:
        return self.segments[-1]

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryMonthlyMap":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("map_id"):
            values["map_id"] = "complete_history_monthly_map_" + canonical_sha256(
                {
                    "role": _json_value(values.get("role")),
                    "outer_parent": _json_value(values.get("outer_parent")),
                    "segments": _json_value(values.get("segments")),
                    "as_of_monthly_candle": _json_value(values.get("as_of_monthly_candle")),
                    "source_packet_hash": values.get("source_packet_hash"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_monthly_map_content_hash(result))


def complete_history_monthly_map_content_hash(value: CompleteHistoryMonthlyMap | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryMonthlyMap) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryStageRequest(_JsonContract):
    request_id: str
    role: RecursiveProofHypothesisRole
    parent_wave: ProofWaveSnapshot
    stage: CompleteHistoryShadowStage
    target_child_degree: str
    target_timeframe: str
    native_window: NativeOHLCVWindow
    pivot_catalog: tuple[TypedPivot, ...]
    parent_boundary_lineage: tuple[ProofBoundaryLineage, ...]
    plan_content_hash: str
    policy_content_hash: str
    analysis_cutoff_utc: str
    created_at_utc: str
    blind_candidate_rules_pack: BlindCandidateRulesPack = field(default_factory=default_blind_candidate_rules_pack)
    schema_version: str = COMPLETE_HISTORY_STAGE_REQUEST_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, field_name="request_id"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        if not isinstance(self.parent_wave, ProofWaveSnapshot):
            raise TypeError("parent_wave must be a ProofWaveSnapshot.")
        object.__setattr__(self, "stage", _enum(self.stage, CompleteHistoryShadowStage, field_name="stage"))
        if self.target_child_degree not in STANDARD_DEGREES or self.target_child_degree == "Unassigned":
            raise ValueError("target_child_degree must be an assigned Elliott degree.")
        object.__setattr__(self, "target_timeframe", _text(self.target_timeframe, field_name="target_timeframe"))
        if self.target_timeframe not in COMPLETE_HISTORY_PROOF_LADDER:
            raise ValueError("target_timeframe must be a rung of the fixed proof ladder.")
        if not isinstance(self.native_window, NativeOHLCVWindow) or not self.native_window.is_native:
            raise ValueError("Stage requests require a native immutable OHLCV window.")
        if self.native_window.timeframe != self.target_timeframe:
            raise ValueError("Stage request native window timeframe must match its target timeframe.")
        if isinstance(self.pivot_catalog, (str, bytes)) or not isinstance(self.pivot_catalog, Sequence):
            raise TypeError("pivot_catalog must be a sequence.")
        pivots = tuple(self.pivot_catalog)
        if not pivots or not all(isinstance(item, TypedPivot) for item in pivots):
            raise ValueError("pivot_catalog must contain typed native pivots.")
        if len({item.pivot_id for item in pivots}) != len(pivots):
            raise ValueError("pivot_catalog cannot contain duplicate IDs.")
        if any(item.source_window_hash != self.native_window.native_rows_hash for item in pivots):
            raise ValueError("Every stage pivot must retain native-window lineage.")
        object.__setattr__(self, "pivot_catalog", pivots)
        if isinstance(self.parent_boundary_lineage, (str, bytes)) or not isinstance(self.parent_boundary_lineage, Sequence):
            raise TypeError("parent_boundary_lineage must be a sequence.")
        boundary_lineage = tuple(self.parent_boundary_lineage)
        if len(boundary_lineage) != 2 or not all(isinstance(item, ProofBoundaryLineage) for item in boundary_lineage):
            raise ValueError("Stage requests require exactly two typed parent/child boundary lineage mappings.")
        if len({item.lineage_id for item in boundary_lineage}) != 2:
            raise ValueError("Stage boundary lineage cannot contain duplicate IDs.")
        expected_parent_ids = {self.parent_wave.start_pivot.pivot_id, self.parent_wave.end_pivot.pivot_id}
        if {item.parent_pivot.pivot_id for item in boundary_lineage} != expected_parent_ids:
            raise ValueError("Stage boundary lineage must map the parent start and end pivots exactly once.")
        child_catalog = {item.pivot_id: item for item in pivots}
        for item in boundary_lineage:
            child = child_catalog.get(item.child_pivot.pivot_id)
            if child is None or child.content_hash != item.child_pivot.content_hash:
                raise ValueError("Stage boundary lineage child pivot is not in the target native catalog.")
        object.__setattr__(self, "parent_boundary_lineage", boundary_lineage)
        for name in ("plan_content_hash", "policy_content_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if not isinstance(self.blind_candidate_rules_pack, BlindCandidateRulesPack):
            raise TypeError("blind_candidate_rules_pack must be a BlindCandidateRulesPack.")
        if self.blind_candidate_rules_pack.content_hash != blind_candidate_rules_pack_content_hash(self.blind_candidate_rules_pack):
            raise ValueError("blind_candidate_rules_pack has an invalid canonical hash.")
        object.__setattr__(self, "analysis_cutoff_utc", _utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_stage_request_content_hash(self):
            raise ValueError("CompleteHistoryStageRequest content_hash does not match its payload.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "role": self.role.value,
            "parent_wave": self.parent_wave.to_dict(),
            "stage": self.stage.value,
            "target_child_degree": self.target_child_degree,
            "target_timeframe": self.target_timeframe,
            "native_window_hash": self.native_window.content_hash,
            "pivot_catalog_hash": canonical_sha256([item.to_dict() for item in self.pivot_catalog]),
            "parent_boundary_lineage": [item.to_dict() for item in self.parent_boundary_lineage],
            "plan_content_hash": self.plan_content_hash,
            "policy_content_hash": self.policy_content_hash,
            "blind_candidate_rules_pack": self.blind_candidate_rules_pack.to_dict(),
            "analysis_cutoff_utc": self.analysis_cutoff_utc,
            "created_at_utc": self.created_at_utc,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryStageRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("request_id"):
            values["request_id"] = "complete_history_stage_" + canonical_sha256(
                {
                    "role": _json_value(values.get("role")),
                    "parent": _json_value(values.get("parent_wave")),
                    "stage": _json_value(values.get("stage")),
                    "window": _json_value(values.get("native_window")),
                    "parent_boundary_lineage": _json_value(values.get("parent_boundary_lineage")),
                    "plan": values.get("plan_content_hash"),
                    "rules_pack": _json_value(
                        values.get("blind_candidate_rules_pack", default_blind_candidate_rules_pack())
                    ),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_stage_request_content_hash(result))


def complete_history_stage_request_content_hash(value: CompleteHistoryStageRequest | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryStageRequest) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") == "complete-history-stage-request-1.0.0":
        payload.pop("parent_boundary_lineage", None)
    if payload.get("schema_version") in {
        "complete-history-stage-request-1.0.0",
        "complete-history-stage-request-1.1.0",
    }:
        payload.pop("blind_candidate_rules_pack", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryWeeklyBatchRequest(_JsonContract):
    """One role-isolated Weekly request for every completed eligible Monthly segment."""

    batch_id: str
    role: RecursiveProofHypothesisRole
    monthly_map_content_hash: str
    segment_requests: tuple[CompleteHistoryStageRequest, ...]
    plan_content_hash: str
    policy_content_hash: str
    analysis_cutoff_utc: str
    created_at_utc: str
    blind_candidate_rules_pack: BlindCandidateRulesPack = field(default_factory=default_blind_candidate_rules_pack)
    schema_version: str = COMPLETE_HISTORY_WEEKLY_BATCH_REQUEST_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _text(self.batch_id, field_name="batch_id"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        object.__setattr__(self, "monthly_map_content_hash", _hash(self.monthly_map_content_hash, field_name="monthly_map_content_hash"))
        if isinstance(self.segment_requests, (str, bytes)) or not isinstance(self.segment_requests, Sequence):
            raise TypeError("segment_requests must be a sequence.")
        requests = tuple(self.segment_requests)
        if not requests or len(requests) >= _MAXIMUM_MONTHLY_MAP_SEGMENTS:
            raise ValueError("A Weekly batch must contain the bounded completed Monthly segment request set.")
        if not all(isinstance(item, CompleteHistoryStageRequest) for item in requests):
            raise TypeError("segment_requests must contain CompleteHistoryStageRequest objects.")
        if any(
            item.role is not self.role
            or item.stage is not CompleteHistoryShadowStage.WEEKLY_CHILDREN
            or item.target_timeframe != "weekly"
            or item.parent_wave.completion_state != "completed"
            for item in requests
        ):
            raise ValueError("Weekly batch requests must target completed Monthly parents for the same role.")
        if len({item.parent_wave.wave_id for item in requests}) != len(requests):
            raise ValueError("A Weekly batch cannot repeat one Monthly segment.")
        starts = tuple(_utc_datetime(item.parent_wave.start_pivot.timestamp_utc) for item in requests)
        if starts != tuple(sorted(starts)):
            raise ValueError("Weekly batch Monthly segments must remain in deterministic map order.")
        for name in ("plan_content_hash", "policy_content_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        analysis_cutoff_utc = _utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc")
        if any(
            item.plan_content_hash != self.plan_content_hash
            or item.policy_content_hash != self.policy_content_hash
            or item.analysis_cutoff_utc != analysis_cutoff_utc
            for item in requests
        ):
            raise ValueError("Weekly batch requests must share one plan, policy, and cutoff.")
        if not isinstance(self.blind_candidate_rules_pack, BlindCandidateRulesPack):
            raise TypeError("blind_candidate_rules_pack must be a BlindCandidateRulesPack.")
        if self.blind_candidate_rules_pack.content_hash != blind_candidate_rules_pack_content_hash(self.blind_candidate_rules_pack):
            raise ValueError("blind_candidate_rules_pack has an invalid canonical hash.")
        if any(item.blind_candidate_rules_pack.content_hash != self.blind_candidate_rules_pack.content_hash for item in requests):
            raise ValueError("Every Weekly segment request must retain the batch rules-pack hash.")
        object.__setattr__(self, "segment_requests", requests)
        object.__setattr__(self, "analysis_cutoff_utc", analysis_cutoff_utc)
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_weekly_batch_request_content_hash(self):
            raise ValueError("CompleteHistoryWeeklyBatchRequest content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryWeeklyBatchRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("batch_id"):
            values["batch_id"] = "complete_history_weekly_batch_" + canonical_sha256(
                {
                    "role": _json_value(values.get("role")),
                    "monthly_map": values.get("monthly_map_content_hash"),
                    "segment_requests": _json_value(values.get("segment_requests")),
                    "plan": values.get("plan_content_hash"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_weekly_batch_request_content_hash(result))


def complete_history_weekly_batch_request_content_hash(
    value: CompleteHistoryWeeklyBatchRequest | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryWeeklyBatchRequest) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryWeeklyChildGraphBatch(_JsonContract):
    """Normalized candidate-only Weekly graphs, keyed by completed Monthly segment."""

    batch_id: str
    role: RecursiveProofHypothesisRole
    batch_request_content_hash: str
    graphs: tuple[CandidateChildGraph, ...]
    provider: str
    model: str | None
    generated_at_utc: str
    candidate_state: str = "candidate"
    schema_version: str = COMPLETE_HISTORY_WEEKLY_BATCH_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _text(self.batch_id, field_name="batch_id"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        object.__setattr__(self, "batch_request_content_hash", _hash(self.batch_request_content_hash, field_name="batch_request_content_hash"))
        if isinstance(self.graphs, (str, bytes)) or not isinstance(self.graphs, Sequence):
            raise TypeError("graphs must be a sequence.")
        graphs = tuple(self.graphs)
        if not graphs or not all(isinstance(item, CandidateChildGraph) for item in graphs):
            raise ValueError("Weekly batch graphs must contain candidate-only child graphs.")
        if len({item.parent_candidate_id for item in graphs}) != len(graphs):
            raise ValueError("Weekly batch graphs cannot repeat a Monthly parent segment.")
        if any(
            item.role.value != self.role.value
            or item.target_timeframe != "weekly"
            or any(child.candidate_state != "candidate" for child in item.children)
            or item.proof_scope.verified_to_timeframe is not None
            for item in graphs
        ):
            raise ValueError("Weekly batch graphs must remain candidate-only Weekly outputs for one role.")
        object.__setattr__(self, "graphs", graphs)
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        object.__setattr__(self, "generated_at_utc", _utc(self.generated_at_utc, field_name="generated_at_utc"))
        if self.candidate_state != "candidate":
            raise ValueError("Weekly graph batches must remain candidate-only.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_weekly_child_graph_batch_content_hash(self):
            raise ValueError("CompleteHistoryWeeklyChildGraphBatch content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryWeeklyChildGraphBatch":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("batch_id"):
            values["batch_id"] = "complete_history_weekly_graph_batch_" + canonical_sha256(
                {
                    "role": _json_value(values.get("role")),
                    "request": values.get("batch_request_content_hash"),
                    "graphs": _json_value(values.get("graphs")),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_weekly_child_graph_batch_content_hash(result))


def complete_history_weekly_child_graph_batch_content_hash(
    value: CompleteHistoryWeeklyChildGraphBatch | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryWeeklyChildGraphBatch) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistorySegmentProofResult(_JsonContract):
    """Deterministic proof result for one completed map segment, never a map verdict."""

    role: RecursiveProofHypothesisRole
    monthly_map_content_hash: str
    segment_id: str
    weekly_batch_content_hash: str | None
    proof_result: RecursiveProofBundleResult
    schema_version: str = COMPLETE_HISTORY_SEGMENT_PROOF_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        object.__setattr__(self, "monthly_map_content_hash", _hash(self.monthly_map_content_hash, field_name="monthly_map_content_hash"))
        object.__setattr__(self, "segment_id", _text(self.segment_id, field_name="segment_id"))
        if self.weekly_batch_content_hash is not None:
            object.__setattr__(self, "weekly_batch_content_hash", _hash(self.weekly_batch_content_hash, field_name="weekly_batch_content_hash"))
        if not isinstance(self.proof_result, RecursiveProofBundleResult):
            raise TypeError("proof_result must be a RecursiveProofBundleResult.")
        if self.proof_result.role is not self.role:
            raise ValueError("Segment proof result role must match its proof bundle role.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_segment_proof_result_content_hash(self):
            raise ValueError("CompleteHistorySegmentProofResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistorySegmentProofResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=complete_history_segment_proof_result_content_hash(result))


def complete_history_segment_proof_result_content_hash(
    value: CompleteHistorySegmentProofResult | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistorySegmentProofResult) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryStage1ScreenResult(_JsonContract):
    """Deterministic Stage-1 scope and hard-rule result.

    The screen does not verify a wave.  It only decides whether the candidate
    graph is structurally eligible to reach a deeper shadow stage.
    """

    scope: str
    passed: bool
    reason_codes: tuple[CompleteHistoryStage1HardRuleCode, ...]
    schema_version: str = "complete-history-stage1-screen-1.0.0"
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _text(self.scope, field_name="scope"))
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean.")
        if isinstance(self.reason_codes, (str, bytes)) or not isinstance(self.reason_codes, Sequence):
            raise TypeError("reason_codes must be a sequence.")
        reasons = tuple(_enum(item, CompleteHistoryStage1HardRuleCode, field_name="reason_codes") for item in self.reason_codes)
        if len(reasons) != len(set(reasons)):
            raise ValueError("reason_codes cannot contain duplicates.")
        if self.passed and reasons:
            raise ValueError("A passed Stage-1 screen cannot carry failure reasons.")
        if not self.passed and not reasons:
            raise ValueError("A failed Stage-1 screen requires deterministic reason codes.")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_stage1_screen_content_hash(self):
            raise ValueError("CompleteHistoryStage1ScreenResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryStage1ScreenResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=complete_history_stage1_screen_content_hash(result))


def complete_history_stage1_screen_content_hash(
    value: CompleteHistoryStage1ScreenResult | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryStage1ScreenResult) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CompleteHistoryShadowExecution(_JsonContract):
    execution_id: str
    plan_content_hash: str
    bundle_content_hash: str
    approval_content_hash: str
    provider: str
    model: str | None
    proof_result: RecursiveProofBundlePairResult
    calls_primary: int
    calls_alternative: int
    warnings: tuple[str, ...]
    created_at_utc: str
    root_candidates: tuple[CompleteHistoryRootCandidate, ...] = ()
    monthly_maps: tuple[CompleteHistoryMonthlyMap, ...] = ()
    segment_proof_results: tuple[CompleteHistorySegmentProofResult, ...] = ()
    max_stage: CompleteHistoryMaximumStage = CompleteHistoryMaximumStage.FOUR_HOUR
    exact_call_limit: int = 64
    artifact_output_directory: str | None = None
    created_artifact_content_hashes: tuple[str, ...] = ()
    reused_artifact_content_hashes: tuple[str, ...] = ()
    attempted_provider_calls: int | None = None
    accepted_provider_calls: int | None = None
    rejected_provider_calls: int | None = None
    schema_version: str = COMPLETE_HISTORY_EXECUTION_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", _text(self.execution_id, field_name="execution_id"))
        for name in ("plan_content_hash", "bundle_content_hash", "approval_content_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, field_name="model"))
        if isinstance(self.root_candidates, (str, bytes)) or not isinstance(self.root_candidates, Sequence):
            raise TypeError("root_candidates must be a sequence.")
        roots = tuple(self.root_candidates)
        if roots and (
            tuple(item.role for item in roots)
            != (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)
        ):
            raise ValueError("Legacy execution root candidates must retain one Primary and one Alternative record.")
        if not all(isinstance(item, CompleteHistoryRootCandidate) for item in roots):
            raise TypeError("root_candidates must contain CompleteHistoryRootCandidate objects.")
        object.__setattr__(self, "root_candidates", roots)
        if isinstance(self.monthly_maps, (str, bytes)) or not isinstance(self.monthly_maps, Sequence):
            raise TypeError("monthly_maps must be a sequence.")
        maps = tuple(self.monthly_maps)
        if maps and (
            tuple(item.role for item in maps)
            != (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)
        ):
            raise ValueError("Monthly-map execution must retain one Primary and one Alternative map.")
        if not all(isinstance(item, CompleteHistoryMonthlyMap) for item in maps):
            raise TypeError("monthly_maps must contain CompleteHistoryMonthlyMap objects.")
        if roots and maps:
            raise ValueError("One execution cannot mix legacy roots and active-parent Monthly maps.")
        if self.schema_version == COMPLETE_HISTORY_EXECUTION_SCHEMA_VERSION and not maps:
            raise ValueError("Active-parent Monthly-map executions require both isolated Monthly maps.")
        object.__setattr__(self, "monthly_maps", maps)
        if isinstance(self.segment_proof_results, (str, bytes)) or not isinstance(self.segment_proof_results, Sequence):
            raise TypeError("segment_proof_results must be a sequence.")
        segment_results = tuple(self.segment_proof_results)
        if not all(isinstance(item, CompleteHistorySegmentProofResult) for item in segment_results):
            raise TypeError("segment_proof_results must contain CompleteHistorySegmentProofResult objects.")
        if maps:
            map_by_role = {item.role: item for item in maps}
            if any(
                item.monthly_map_content_hash != map_by_role[item.role].content_hash
                or item.segment_id not in {segment.wave_id for segment in map_by_role[item.role].completed_segments}
                for item in segment_results
            ):
                raise ValueError("Segment proof results must point to completed segments in their immutable Monthly map.")
        elif segment_results:
            raise ValueError("Legacy root executions cannot carry active-parent segment proof results.")
        object.__setattr__(self, "segment_proof_results", segment_results)
        if not isinstance(self.proof_result, RecursiveProofBundlePairResult):
            raise TypeError("proof_result must be a RecursiveProofBundlePairResult.")
        object.__setattr__(self, "calls_primary", _int(self.calls_primary, field_name="calls_primary", minimum=0))
        object.__setattr__(self, "calls_alternative", _int(self.calls_alternative, field_name="calls_alternative", minimum=0))
        legacy_attempted = self.calls_primary + self.calls_alternative
        attempted = legacy_attempted if self.attempted_provider_calls is None else _int(
            self.attempted_provider_calls,
            field_name="attempted_provider_calls",
            minimum=0,
        )
        accepted = attempted if self.accepted_provider_calls is None else _int(
            self.accepted_provider_calls,
            field_name="accepted_provider_calls",
            minimum=0,
        )
        rejected = (attempted - accepted) if self.rejected_provider_calls is None else _int(
            self.rejected_provider_calls,
            field_name="rejected_provider_calls",
            minimum=0,
        )
        if attempted != legacy_attempted:
            raise ValueError("attempted_provider_calls must equal calls_primary plus calls_alternative.")
        if accepted + rejected != attempted:
            raise ValueError("accepted_provider_calls plus rejected_provider_calls must equal attempted_provider_calls.")
        object.__setattr__(self, "attempted_provider_calls", attempted)
        object.__setattr__(self, "accepted_provider_calls", accepted)
        object.__setattr__(self, "rejected_provider_calls", rejected)
        object.__setattr__(self, "max_stage", _enum(self.max_stage, CompleteHistoryMaximumStage, field_name="max_stage"))
        object.__setattr__(self, "exact_call_limit", _int(self.exact_call_limit, field_name="exact_call_limit", minimum=1))
        if self.calls_primary + self.calls_alternative > self.exact_call_limit:
            raise ValueError("Execution cannot exceed its approved exact_call_limit.")
        if self.artifact_output_directory is not None:
            object.__setattr__(self, "artifact_output_directory", _text(self.artifact_output_directory, field_name="artifact_output_directory"))
        for name in ("created_artifact_content_hashes", "reused_artifact_content_hashes"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise TypeError(f"{name} must be a sequence.")
            hashes = tuple(_hash(item, field_name=name) for item in value)
            if len(hashes) != len(set(hashes)):
                raise ValueError(f"{name} cannot contain duplicates.")
            object.__setattr__(self, name, hashes)
        if set(self.created_artifact_content_hashes) & set(self.reused_artifact_content_hashes):
            raise ValueError("Created and reused artifact hashes must be disjoint.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        object.__setattr__(self, "warnings", tuple(_text(item, field_name="warnings") for item in self.warnings))
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_shadow_execution_content_hash(self):
            raise ValueError("CompleteHistoryShadowExecution content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryShadowExecution":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("execution_id"):
            values["execution_id"] = "complete_history_execution_" + canonical_sha256(
                {
                    "plan": values.get("plan_content_hash"),
                    "roots": _json_value(values.get("root_candidates")),
                    "monthly_maps": _json_value(values.get("monthly_maps")),
                    "segment_proof_results": _json_value(values.get("segment_proof_results")),
                    "proof": _json_value(values.get("proof_result")),
                    "max_stage": _json_value(values.get("max_stage", CompleteHistoryMaximumStage.FOUR_HOUR)),
                    "exact_call_limit": values.get("exact_call_limit", 64),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_shadow_execution_content_hash(result))


def complete_history_shadow_execution_content_hash(value: CompleteHistoryShadowExecution | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryShadowExecution) else dict(value)
    payload.pop("content_hash", None)
    # Existing 1.1.0 execution records only exposed attempted calls through
    # calls_primary/calls_alternative. Keep their stored hashes readable after
    # adding explicit accepted/rejected accounting in 1.2.0.
    if payload.get("schema_version") == "complete-history-shadow-execution-1.1.0":
        payload.pop("attempted_provider_calls", None)
        payload.pop("accepted_provider_calls", None)
        payload.pop("rejected_provider_calls", None)
    if payload.get("schema_version") in {
        "complete-history-shadow-execution-1.1.0",
        "complete-history-shadow-execution-1.2.0",
    }:
        payload.pop("monthly_maps", None)
        payload.pop("segment_proof_results", None)
    return canonical_sha256(payload)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


@dataclass(frozen=True, slots=True)
class CompleteHistoryStageArtifact(_JsonContract):
    """One immutable, structured provider result saved outside project state.

    The payload stores only the strict structured output that has already
    passed the candidate parser.  Raw provider traces, hidden reasoning,
    credentials, and requests containing source rows are never written.
    """

    artifact_id: str
    stage: CompleteHistoryMaximumStage
    role: RecursiveProofHypothesisRole
    plan_content_hash: str
    approval_content_hash: str
    bundle_content_hash: str
    max_stage: CompleteHistoryMaximumStage
    exact_call_limit: int
    stage_input_hash: str
    lineage_parent_artifact_hash: str | None
    output_kind: CompleteHistoryStageArtifactOutputKind
    structured_output: Mapping[str, Any]
    structured_output_hash: str
    normalized_output_content_hash: str
    created_at_utc: str
    rules_pack_content_hash: str = field(
        default_factory=lambda: default_blind_candidate_rules_pack().content_hash
    )
    schema_version: str = COMPLETE_HISTORY_STAGE_ARTIFACT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _text(self.artifact_id, field_name="artifact_id"))
        object.__setattr__(self, "stage", _enum(self.stage, CompleteHistoryMaximumStage, field_name="stage"))
        object.__setattr__(self, "role", _enum(self.role, RecursiveProofHypothesisRole, field_name="role"))
        for name in (
            "plan_content_hash",
            "approval_content_hash",
            "bundle_content_hash",
            "stage_input_hash",
            "structured_output_hash",
            "normalized_output_content_hash",
            "rules_pack_content_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if self.lineage_parent_artifact_hash is not None:
            object.__setattr__(
                self,
                "lineage_parent_artifact_hash",
                _hash(self.lineage_parent_artifact_hash, field_name="lineage_parent_artifact_hash"),
            )
        object.__setattr__(self, "max_stage", _enum(self.max_stage, CompleteHistoryMaximumStage, field_name="max_stage"))
        if _maximum_stage_index(self.stage) > _maximum_stage_index(self.max_stage):
            raise ValueError("Artifact stage cannot exceed the approved max_stage.")
        object.__setattr__(self, "exact_call_limit", _int(self.exact_call_limit, field_name="exact_call_limit", minimum=1))
        object.__setattr__(self, "output_kind", _enum(self.output_kind, CompleteHistoryStageArtifactOutputKind, field_name="output_kind"))
        schema_version = _text(self.schema_version, field_name="schema_version")
        if schema_version == COMPLETE_HISTORY_STAGE_ARTIFACT_SCHEMA_VERSION:
            expected_kind = (
                CompleteHistoryStageArtifactOutputKind.MONTHLY_MAP
                if self.stage is CompleteHistoryMaximumStage.MONTHLY
                else CompleteHistoryStageArtifactOutputKind.WEEKLY_CHILD_GRAPH_BATCH
                if self.stage is CompleteHistoryMaximumStage.WEEKLY
                else CompleteHistoryStageArtifactOutputKind.CHILD_GRAPH
            )
        else:
            expected_kind = (
                CompleteHistoryStageArtifactOutputKind.ROOT_CANDIDATE
                if self.stage is CompleteHistoryMaximumStage.MONTHLY
                else CompleteHistoryStageArtifactOutputKind.CHILD_GRAPH
            )
        if self.output_kind is not expected_kind:
            raise ValueError("Artifact output_kind is incompatible with its stage.")
        if not isinstance(self.structured_output, Mapping):
            raise TypeError("structured_output must be an object.")
        structured_output = _freeze_json(self.structured_output)
        if not isinstance(structured_output, Mapping):
            raise TypeError("structured_output must remain an object after canonicalization.")
        object.__setattr__(self, "structured_output", structured_output)
        if canonical_sha256(structured_output) != self.structured_output_hash:
            raise ValueError("Artifact structured_output_hash does not match the structured output.")
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", schema_version)
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != complete_history_stage_artifact_content_hash(self):
            raise ValueError("CompleteHistoryStageArtifact content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CompleteHistoryStageArtifact":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("artifact_id"):
            values["artifact_id"] = "complete_history_stage_artifact_" + canonical_sha256(
                {
                    "stage": _json_value(values.get("stage")),
                    "role": _json_value(values.get("role")),
                    "plan": values.get("plan_content_hash"),
                    "approval": values.get("approval_content_hash"),
                    "input": values.get("stage_input_hash"),
                    "parent": values.get("lineage_parent_artifact_hash"),
                    "rules_pack_content_hash": values.get(
                        "rules_pack_content_hash",
                        default_blind_candidate_rules_pack().content_hash,
                    ),
                    "output": _json_value(values.get("structured_output")),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=complete_history_stage_artifact_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "CompleteHistoryStageArtifact":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != complete_history_stage_artifact_content_hash(result):
            raise ValueError("CompleteHistoryStageArtifact content_hash does not match its payload.")
        return result


def complete_history_stage_artifact_content_hash(value: CompleteHistoryStageArtifact | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CompleteHistoryStageArtifact) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") in {
        "complete-history-stage-artifact-1.0.0",
        "complete-history-stage-artifact-1.1.0",
    }:
        payload.pop("rules_pack_content_hash", None)
    return canonical_sha256(payload)


class CompleteHistoryLocalArtifactStore:
    """Content-addressed local artifact writer with deterministic resume lookup."""

    _PREFIX = "complete_history_stage_"

    def __init__(self, output_directory: str | Path, *, bundle_directory: str | Path) -> None:
        self.directory = Path(output_directory).expanduser().resolve()
        source_directory = Path(bundle_directory).resolve()
        try:
            self.directory.relative_to(source_directory)
        except ValueError:
            pass
        else:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_DIRECTORY_INVALID,
                "Artifact output_directory cannot be the immutable input bundle or one of its descendants.",
            )
        if self.directory.exists() and not self.directory.is_dir():
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_DIRECTORY_INVALID,
                "Artifact output_directory exists but is not a directory.",
            )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_DIRECTORY_INVALID,
                "Artifact output_directory could not be created.",
            ) from exc

    def _path(self, artifact: CompleteHistoryStageArtifact) -> Path:
        return self.directory / f"{self._PREFIX}{artifact.content_hash}.json"

    def _read_path(self, path: Path) -> CompleteHistoryStageArtifact:
        try:
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("Artifact JSON must be an object.")
            if payload != _canonical_json_bytes(value):
                raise ValueError("Artifact file is not canonical JSON.")
            artifact = CompleteHistoryStageArtifact.from_dict(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                f"Artifact {path.name} is unreadable, non-canonical, or hash-invalid.",
            ) from exc
        if path.name != f"{self._PREFIX}{artifact.content_hash}.json":
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                "Artifact filename does not match its content hash.",
            )
        return artifact

    def write(self, artifact: CompleteHistoryStageArtifact) -> Path:
        if not isinstance(artifact, CompleteHistoryStageArtifact):
            raise TypeError("artifact must be a CompleteHistoryStageArtifact.")
        if artifact.content_hash != complete_history_stage_artifact_content_hash(artifact):
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                "Artifact content hash is invalid before persistence.",
            )
        destination = self._path(artifact)
        expected_bytes = _canonical_json_bytes(artifact.to_dict())
        if destination.exists():
            existing = self._read_path(destination)
            if existing.content_hash != artifact.content_hash or destination.read_bytes() != expected_bytes:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.ARTIFACT_LINEAGE_CONFLICT,
                    "A different artifact already occupies the requested content-addressed path.",
                )
            return destination
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".complete_history_stage_",
                suffix=".tmp",
                dir=self.directory,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(expected_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # A same-directory hard link is an atomic create-without-
                # replacement operation.  If another process created the
                # identical content-addressed file first, we validate and
                # reuse it instead of overwriting it.
                os.link(temporary_path, destination)
            except FileExistsError:
                existing = self._read_path(destination)
                if existing.content_hash != artifact.content_hash or destination.read_bytes() != expected_bytes:
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_LINEAGE_CONFLICT,
                        "Concurrent artifact creation produced incompatible content.",
                    )
            except OSError as exc:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.ARTIFACT_WRITE_FAILED,
                    "Artifact storage does not support an atomic no-overwrite write.",
                ) from exc
        except CompleteHistoryShadowError:
            raise
        except OSError as exc:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_WRITE_FAILED,
                "Artifact could not be written atomically.",
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return destination

    def find(
        self,
        *,
        stage: CompleteHistoryMaximumStage,
        role: RecursiveProofHypothesisRole,
        plan_content_hash: str,
        approval_content_hash: str,
        bundle_content_hash: str,
        max_stage: CompleteHistoryMaximumStage,
        exact_call_limit: int,
        stage_input_hash: str,
        lineage_parent_artifact_hash: str | None,
    ) -> CompleteHistoryStageArtifact | None:
        matches: list[CompleteHistoryStageArtifact] = []
        for path in sorted(self.directory.glob(f"{self._PREFIX}*.json")):
            artifact = self._read_path(path)
            if (
                artifact.stage is stage
                and artifact.role is role
                and artifact.plan_content_hash == plan_content_hash
                and artifact.approval_content_hash == approval_content_hash
                and artifact.bundle_content_hash == bundle_content_hash
                and artifact.max_stage is max_stage
                and artifact.exact_call_limit == exact_call_limit
                and artifact.stage_input_hash == stage_input_hash
                and artifact.lineage_parent_artifact_hash == lineage_parent_artifact_hash
            ):
                matches.append(artifact)
        if len(matches) > 1:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ARTIFACT_LINEAGE_CONFLICT,
                "More than one immutable artifact matches one stage input and lineage position.",
            )
        return matches[0] if matches else None


class CompleteHistoryCandidateProvider(Protocol):
    """Provider boundary for a deliberately future, manually approved call."""

    name: str
    model: str | None

    def generate_monthly_map(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def generate_weekly_child_graph_batch(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    # Legacy methods remain available for historical adapters and artifacts.
    def generate_monthly_root_candidate(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def generate_child_graph(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_id",
            "degree",
            "declared_family",
            "direction",
            "completion_state",
            "start_pivot_id",
            "end_pivot_id",
            "origin_control",
            "invalidation",
        ],
        "properties": {
            "candidate_id": {"type": "string", "minLength": 1},
            "degree": {"type": "string", "enum": [item for item in STANDARD_DEGREES if item != "Unassigned"]},
            "declared_family": {"type": "string", "minLength": 1},
            "direction": {"type": "string", "enum": ("up", "down")},
            "completion_state": {"type": "string", "enum": ("completed", "active", "projected")},
            "start_pivot_id": {"type": "string", "minLength": 1},
            "end_pivot_id": {"type": "string", "minLength": 1},
            "origin_control": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_pivot_id", "end_pivot_id"],
                        "properties": {
                            "start_pivot_id": {"type": "string", "minLength": 1},
                            "end_pivot_id": {"type": "string", "minLength": 1},
                        },
                    },
                ]
            },
            "invalidation": candidate_invalidation_openai_json_schema(),
        },
    }
)


MONTHLY_MAP_OUTPUT_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["map_id", "outer_parent", "origin_control", "as_of_monthly_candle_id", "segments"],
        "properties": {
            "map_id": {"type": "string", "minLength": 1},
            "outer_parent": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "parent_id",
                    "degree",
                    "declared_family",
                    "direction",
                    "completion_state",
                    "start_pivot_id",
                    "end_pivot_id",
                    "invalidation",
                ],
                "properties": {
                    "parent_id": {"type": "string", "minLength": 1},
                    "degree": {"type": "string", "enum": [item for item in STANDARD_DEGREES if item != "Unassigned"]},
                    "declared_family": {"type": "string", "minLength": 1},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "completion_state": {"type": "string", "enum": ["active"]},
                    "start_pivot_id": {"type": "string", "minLength": 1},
                    "end_pivot_id": {"type": "string", "minLength": 1},
                    "invalidation": candidate_invalidation_openai_json_schema(),
                },
            },
            "origin_control": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_pivot_id", "end_pivot_id"],
                        "properties": {
                            "start_pivot_id": {"type": "string", "minLength": 1},
                            "end_pivot_id": {"type": "string", "minLength": 1},
                        },
                    },
                ]
            },
            "as_of_monthly_candle_id": {"type": "string", "minLength": 1},
            "segments": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAXIMUM_MONTHLY_MAP_SEGMENTS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "segment_id",
                        "degree",
                        "declared_family",
                        "direction",
                        "completion_state",
                        "start_pivot_id",
                        "end_pivot_id",
                        "invalidation",
                    ],
                    "properties": {
                        "segment_id": {"type": "string", "minLength": 1},
                        "degree": {"type": "string", "enum": [item for item in STANDARD_DEGREES if item != "Unassigned"]},
                        "declared_family": {"type": "string", "minLength": 1},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                        "completion_state": {"type": "string", "enum": ["completed", "active"]},
                        "start_pivot_id": {"type": "string", "minLength": 1},
                        "end_pivot_id": {"type": "string", "minLength": 1},
                        "invalidation": candidate_invalidation_openai_json_schema(),
                    },
                },
            },
        },
    }
)


CHILD_GRAPH_OUTPUT_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["graph_id", "declared_family", "boundary_lineage", "children"],
        "properties": {
            "graph_id": {"type": "string", "minLength": 1},
            "declared_family": {"type": "string", "minLength": 1},
            "boundary_lineage": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["lineage_id", "parent_pivot_id", "child_pivot_id", "mapping_kind"],
                    "properties": {
                        "lineage_id": {"type": "string", "minLength": 1},
                        "parent_pivot_id": {"type": "string", "minLength": 1},
                        "child_pivot_id": {"type": "string", "minLength": 1},
                        "mapping_kind": {"type": "string", "enum": ["exact_price_source_candle_bracket"]},
                    },
                },
            },
            "children": {
                "type": "array",
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
                    ],
                    "properties": {
                        "child_id": {"type": "string", "minLength": 1},
                        "sequence_position": {"type": "string", "minLength": 1},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                        "declared_family": {"type": "string", "minLength": 1},
                        "start_pivot_id": {"type": "string", "minLength": 1},
                        "end_pivot_id": {"type": "string", "minLength": 1},
                        "invalidation": candidate_invalidation_openai_json_schema(),
                    },
                },
            },
        },
    }
)


WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["batch_id", "graphs"],
        "properties": {
            "batch_id": {"type": "string", "minLength": 1},
            "graphs": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAXIMUM_MONTHLY_MAP_SEGMENTS - 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["segment_id", "graph"],
                    "properties": {
                        "segment_id": {"type": "string", "minLength": 1},
                        "graph": CHILD_GRAPH_OUTPUT_SCHEMA,
                    },
                },
            },
        },
    }
)


def _metadata(value: Mapping[str, Any], *, filename: str) -> Mapping[str, Any]:
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
            f"{filename} does not contain a metadata object.",
        )
    return metadata


def _dataset_from_document(
    *,
    bundle_id: str,
    timeframe: str,
    path: Path,
    source_file_hash: str,
    canonical_content_hash: str,
    document: Mapping[str, Any],
) -> CompleteHistoryDataset:
    metadata = _metadata(document, filename=path.name)
    required_equal = {
        "timeframe": timeframe,
        "provider": "twelve_data",
        "requested_symbol": "NASDAQ:GOOGL",
        "resolved_symbol": "GOOGL",
        "exchange": "NASDAQ",
        "session": "regular",
        "timezone": "UTC",
        "requested_adjustment": "splits",
        "price_basis": "split_adjusted_dividend_unadjusted_ohlcv",
        "native_or_resampled": "native_provider_candles",
    }
    for field_name, expected in required_equal.items():
        if metadata.get(field_name) != expected:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
                f"{path.name} has incompatible {field_name!r} metadata.",
            )
    if metadata.get("adjustment_policy") != "split-adjusted, dividend-unadjusted OHLCV":
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
            f"{path.name} does not declare the required split-adjusted, dividend-unadjusted policy.",
        )
    if metadata.get("completed_candles_only") is not True or metadata.get("final_candle_completed") is not True:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_CUTOFF_INVALID,
            f"{path.name} is not explicitly completed-candle-only.",
        )
    if metadata.get("validation_status") != "data_contract_passed":
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
            f"{path.name} does not carry a passed data-contract validation status.",
        )
    raw_feed_identity = _text(metadata.get("feed_identity"), field_name=f"{path.name}.feed_identity")
    proof_feed_identity = _normalised_proof_feed_identity(
        raw_feed_identity,
        timeframe=timeframe,
        provider="twelve_data",
        exchange="NASDAQ",
        provider_symbol="GOOGL",
        adjustment="splits",
        session="regular",
    )
    candles_raw = document.get("candles")
    if isinstance(candles_raw, (str, bytes)) or not isinstance(candles_raw, Sequence) or not candles_raw:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
            f"{path.name} has no candle rows.",
        )
    candles: list[NativeCandle] = []
    for index, raw_row in enumerate(candles_raw):
        if not isinstance(raw_row, Mapping):
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
                f"{path.name} contains a non-object candle row.",
            )
        try:
            candles.append(
                NativeCandle.create(
                    candle_id=f"{bundle_id}:{timeframe}:{index}",
                    timestamp_utc=raw_row["date"],
                    open=raw_row["open"],
                    high=raw_row["high"],
                    low=raw_row["low"],
                    close=raw_row["close"],
                    volume=raw_row["volume"],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
                f"{path.name} contains an invalid completed OHLCV candle at row {index}.",
            ) from exc
    metadata_count = metadata.get("candle_count")
    if metadata_count != len(candles):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
            f"{path.name} candle_count does not match its immutable rows.",
        )
    first = candles[0].timestamp_utc
    last = candles[-1].timestamp_utc
    if _utc(metadata.get("first_candle_timestamp_utc"), field_name="metadata.first_candle_timestamp_utc") != first:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_COVERAGE_INVALID,
            f"{path.name} first candle does not match metadata.",
        )
    if _utc(metadata.get("last_candle_timestamp_utc"), field_name="metadata.last_candle_timestamp_utc") != last:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_COVERAGE_INVALID,
            f"{path.name} last candle does not match metadata.",
        )
    cutoff = _utc(metadata.get("dataset_cutoff_utc"), field_name="metadata.dataset_cutoff_utc")
    try:
        dataset_cutoff = DatasetCutoff(
            dataset_id=f"{bundle_id}:{timeframe}:{source_file_hash[:16]}",
            source_reference=str(path),
            timeframe=timeframe,
            cutoff_utc=cutoff,
            dataset_hash=source_file_hash,
            hash_scope=DatasetHashScope.SOURCE_DOCUMENT,
            provider="twelve_data",
            feed_identity=proof_feed_identity,
            requested_symbol="NASDAQ:GOOGL",
            resolved_symbol="GOOGL",
            exchange="NASDAQ",
            session="regular",
            timezone="UTC",
            adjustment={"price": "split-adjusted", "dividends": "unadjusted", "requested": "splits"},
            price_basis="split_adjusted_dividend_unadjusted_ohlcv",
            completed_candles_only=True,
            source_document_hash=source_file_hash,
            captured_at_utc=metadata.get("retrieved_at_utc"),
            bar_count=len(candles),
        )
        source_window = NativeOHLCVWindow.create(
            window_id=f"complete_history:{bundle_id}:{timeframe}:{source_file_hash[:16]}",
            dataset_cutoff=dataset_cutoff,
            timeframe=timeframe,
            candles=tuple(candles),
            coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
            is_native=True,
        )
    except (TypeError, ValueError) as exc:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_METADATA_INVALID,
            f"{path.name} could not become an immutable native source window.",
        ) from exc
    return CompleteHistoryDataset.create(
        timeframe=timeframe,
        source_path=str(path),
        source_file_sha256=source_file_hash,
        canonical_content_sha256=canonical_content_hash,
        raw_feed_identity=raw_feed_identity,
        proof_dataset_cutoff=dataset_cutoff,
        coverage_start_utc=first,
        coverage_end_utc=last,
        source_window=source_window,
    )


def _resolve_reference(path_value: Any, *, field_name: str) -> Path:
    try:
        path = Path(_text(path_value, field_name=field_name))
    except ValueError as exc:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, f"{field_name} is invalid.") from exc
    return path


def _validate_identity_bridge(bridge: Mapping[str, Any], *, manifest: Mapping[str, Any]) -> None:
    instrument = bridge.get("instrument")
    excluded = bridge.get("excluded_security")
    policy = bridge.get("provider_policy")
    ticker_history = bridge.get("ticker_history")
    manifest_instrument = manifest.get("instrument")
    if not isinstance(instrument, Mapping) or not isinstance(excluded, Mapping) or not isinstance(policy, Mapping) or not isinstance(ticker_history, Sequence) or not isinstance(manifest_instrument, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.IDENTITY_BRIDGE_INVALID, "Identity bridge is missing required immutable sections.")
    valid = (
        bridge.get("continuity_verdict") == "verified_class_a_continuity"
        and instrument.get("current_symbol") == "NASDAQ:GOOGL"
        and instrument.get("security_class") == "Alphabet Inc. Class A common stock"
        and manifest_instrument.get("symbol") == "NASDAQ:GOOGL"
        and excluded.get("ticker") == "GOOG"
        and excluded.get("security_class") == "Class C capital stock"
        and excluded.get("from_utc") == "2014-04-03T00:00:00Z"
        and policy.get("forbidden_post_2014_symbol_merge") == "GOOG Class C"
        and policy.get("manual_adjustments_performed") is False
        and policy.get("requested_symbol") == "NASDAQ:GOOGL"
        and policy.get("provider_symbol") == "GOOGL"
    )
    has_pre_transition = any(isinstance(item, Mapping) and item.get("ticker") == "GOOG" and item.get("security_class") == "Class A common stock" for item in ticker_history)
    has_post_transition = any(isinstance(item, Mapping) and item.get("ticker") == "GOOGL" and item.get("security_class") == "Class A common stock" for item in ticker_history)
    if not valid or not has_pre_transition or not has_post_transition:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.IDENTITY_BRIDGE_INVALID,
            "GOOG Class A -> GOOGL Class A continuity or GOOG Class C exclusion is not fully verified.",
        )


def _validate_coverage_map(
    coverage_map: Mapping[str, Any],
    *,
    bundle_cutoff_utc: str,
    datasets: Sequence[CompleteHistoryDataset],
) -> None:
    if coverage_map.get("symbol") != "NASDAQ:GOOGL" or _utc(coverage_map.get("shared_forecast_cutoff_utc"), field_name="coverage_map.shared_forecast_cutoff_utc") != bundle_cutoff_utc:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, "Coverage map symbol or shared cutoff is incompatible.")
    constraints = coverage_map.get("proof_constraints")
    items = coverage_map.get("timeframes")
    if not isinstance(constraints, Mapping) or not isinstance(items, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, "Coverage map is missing proof constraints or timeframes.")
    if constraints.get("native_4h_required_for_recursive_4h_proof") is not True or constraints.get("older_structure_status_before_native_4h_start") != "not_covered":
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, "Coverage map does not preserve the mandatory native-4h not-covered rule.")
    for dataset in datasets:
        entry = items.get(dataset.timeframe)
        if not isinstance(entry, Mapping):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, f"Coverage map omits {dataset.timeframe}.")
        if entry.get("candle_count") != len(dataset.source_window.candles):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, f"Coverage map count does not match {dataset.timeframe}.")
        if _utc(entry.get("coverage_start_utc"), field_name=f"coverage_map.{dataset.timeframe}.coverage_start_utc") != dataset.coverage_start_utc:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, f"Coverage map start does not match {dataset.timeframe}.")
        if _utc(entry.get("coverage_end_utc"), field_name=f"coverage_map.{dataset.timeframe}.coverage_end_utc") != dataset.coverage_end_utc:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, f"Coverage map end does not match {dataset.timeframe}.")
        if _utc(entry.get("dataset_cutoff_utc"), field_name=f"coverage_map.{dataset.timeframe}.dataset_cutoff_utc") != dataset.proof_dataset_cutoff.cutoff_utc:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, f"Coverage map cutoff does not match {dataset.timeframe}.")
    four_hour = items.get("4h")
    if not isinstance(four_hour, Mapping) or not isinstance(four_hour.get("proof_coverage"), Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, "Coverage map does not describe 4h proof availability.")
    proof = four_hour["proof_coverage"]
    if proof.get("before_available_from_utc") != "not_covered" or _utc(proof.get("available_from_utc"), field_name="coverage_map.4h.available_from_utc") != next(item for item in datasets if item.timeframe == "4h").coverage_start_utc:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.COVERAGE_MAP_INVALID, "Coverage map does not match the native 4h availability boundary.")


def load_complete_history_bundle(bundle_directory: str | Path) -> CompleteHistoryBundle:
    """Read and verify the immutable GOOGL full-history bundle before planning.

    The function is read-only.  It rejects a single bad hash, an incomplete
    coverage declaration, an incompatible feed, or an invalid Class A bridge
    rather than attempting to repair any historical source.
    """

    directory = Path(bundle_directory).resolve()
    manifest_path = directory / "GOOGL_full_history_bundle_manifest.json"
    if not manifest_path.is_file():
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.REQUIRED_FILE_MISSING, f"Bundle manifest is missing from {directory}.")
    manifest = _read_json(manifest_path)
    manifest_canonical = manifest.get("canonical_content_sha256")
    if not isinstance(manifest_canonical, str) or canonical_sha256(_document_payload_without_canonical_hash(manifest)) != manifest_canonical:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_HASH_INVALID, "Bundle manifest canonical hash is invalid.")
    manifest_file_hash = _file_sha256(manifest_path)
    if manifest.get("kind") != "immutable_full_history_dataset_bundle" or manifest.get("schema_version") != "googl-full-history-bundle-manifest/1.0.0":
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, "Bundle manifest is not the approved immutable full-history format.")
    bundle_id = _text(manifest.get("bundle_id"), field_name="bundle_id")
    shared_cutoff = _utc(manifest.get("shared_forecast_cutoff_utc"), field_name="shared_forecast_cutoff_utc")
    if _utc(manifest.get("source_snapshot_shared_cutoff_utc"), field_name="source_snapshot_shared_cutoff_utc") != shared_cutoff:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.DATASET_CUTOFF_INVALID, "Bundle cutoff differs from its frozen source snapshot cutoff.")
    files = manifest.get("bundle_files")
    sources = manifest.get("immutable_sources")
    if not isinstance(files, Mapping) or not isinstance(sources, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, "Bundle manifest is missing file or source references.")
    for key in ("combined_daily", "coverage_map", "identity_bridge"):
        if not isinstance(files.get(key), Mapping):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, f"Bundle manifest is missing {key} file metadata.")

    def bundle_file(key: str) -> tuple[Path, Mapping[str, Any]]:
        entry = files[key]
        filename = _text(entry.get("filename"), field_name=f"bundle_files.{key}.filename")
        path = (directory / filename).resolve()
        if path.parent != directory:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, f"Bundle file {filename} escapes its immutable directory.")
        document = _verify_document(
            path,
            expected_file_hash=_hash(entry.get("file_sha256"), field_name=f"bundle_files.{key}.file_sha256"),
            expected_canonical_hash=_hash(entry.get("canonical_content_sha256"), field_name=f"bundle_files.{key}.canonical_content_sha256"),
        )
        if entry.get("file_size_bytes") != path.stat().st_size:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.FILE_HASH_INVALID, f"Bundle file size does not match {filename}.")
        return path, document

    daily_path, full_daily = bundle_file("combined_daily")
    coverage_path, coverage_map = bundle_file("coverage_map")
    bridge_path, identity_bridge = bundle_file("identity_bridge")
    _validate_identity_bridge(identity_bridge, manifest=manifest)

    frozen = sources.get("frozen_snapshot")
    extension = sources.get("daily_history_extension")
    if not isinstance(frozen, Mapping) or not isinstance(extension, Mapping) or not isinstance(frozen.get("datasets"), Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, "Bundle has no frozen source snapshot or Daily extension metadata.")
    frozen_datasets = frozen["datasets"]
    for key in COMPLETE_HISTORY_REQUIRED_TIMEFRAMES:
        entry = frozen_datasets.get(key)
        if not isinstance(entry, Mapping):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.REQUIRED_TIMEFRAME_MISSING, f"Frozen source snapshot is missing {key}.")
        source_path = _resolve_reference(entry.get("reference_path"), field_name=f"frozen_snapshot.{key}.reference_path")
        _verify_document(
            source_path,
            expected_file_hash=_hash(entry.get("file_sha256"), field_name=f"frozen_snapshot.{key}.file_sha256"),
            expected_canonical_hash=_hash(entry.get("source_manifest_canonical_content_sha256"), field_name=f"frozen_snapshot.{key}.canonical_content_sha256"),
        )
    source_manifest_path = _resolve_reference(frozen.get("decision_manifest_path"), field_name="frozen_snapshot.decision_manifest_path")
    _verify_document(
        source_manifest_path,
        expected_file_hash=_hash(frozen.get("decision_manifest_file_sha256"), field_name="frozen_snapshot.decision_manifest_file_sha256"),
        expected_canonical_hash=None,
    )
    extension_manifest_path = _resolve_reference(extension.get("manifest_path"), field_name="daily_history_extension.manifest_path")
    _verify_document(
        extension_manifest_path,
        expected_file_hash=_hash(extension.get("manifest_file_sha256"), field_name="daily_history_extension.manifest_file_sha256"),
        expected_canonical_hash=None,
    )

    full_daily_metadata = _metadata(full_daily, filename=daily_path.name)
    if full_daily_metadata.get("identity_bridge_hash") != files["identity_bridge"].get("canonical_content_sha256") or full_daily_metadata.get("shared_forecast_cutoff_utc") != manifest.get("shared_forecast_cutoff_utc"):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.IDENTITY_BRIDGE_INVALID, "Combined Daily source is not bound to this identity bridge and cutoff.")
    components = full_daily_metadata.get("source_components")
    if isinstance(components, (str, bytes)) or not isinstance(components, Sequence) or len(components) != 2:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, "Combined Daily source does not retain exactly two immutable components.")
    component_candle_count = 0
    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MANIFEST_INVALID, "Combined Daily component metadata is invalid.")
        component_candle_count += _int(
            component.get("candle_count"),
            field_name=f"combined_daily.source_components[{index}].candle_count",
            minimum=1,
        )
        component_path = _resolve_reference(component.get("path"), field_name=f"combined_daily.source_components[{index}].path")
        _verify_document(
            component_path,
            expected_file_hash=_hash(component.get("file_sha256"), field_name=f"combined_daily.source_components[{index}].file_sha256"),
            expected_canonical_hash=_hash(component.get("canonical_content_sha256"), field_name=f"combined_daily.source_components[{index}].canonical_content_sha256"),
        )
    if component_candle_count != _int(full_daily_metadata.get("candle_count"), field_name="combined_daily.candle_count", minimum=1):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.DATASET_COVERAGE_INVALID,
            "Combined Daily candle_count does not equal the immutable source-component counts.",
        )

    used_documents: dict[str, tuple[Path, Mapping[str, Any], str, str]] = {
        "daily": (
            daily_path,
            full_daily,
            _hash(files["combined_daily"].get("file_sha256"), field_name="bundle_files.combined_daily.file_sha256"),
            _hash(files["combined_daily"].get("canonical_content_sha256"), field_name="bundle_files.combined_daily.canonical_content_sha256"),
        )
    }
    for timeframe in ("monthly", "weekly", "4h", "1h", "15m"):
        entry = frozen_datasets[timeframe]
        path = _resolve_reference(entry.get("reference_path"), field_name=f"frozen_snapshot.{timeframe}.reference_path")
        document = _verify_document(
            path,
            expected_file_hash=_hash(entry.get("file_sha256"), field_name=f"frozen_snapshot.{timeframe}.file_sha256"),
            expected_canonical_hash=_hash(entry.get("source_manifest_canonical_content_sha256"), field_name=f"frozen_snapshot.{timeframe}.canonical_content_sha256"),
        )
        used_documents[timeframe] = (
            path,
            document,
            _hash(entry.get("file_sha256"), field_name=f"frozen_snapshot.{timeframe}.file_sha256"),
            _hash(entry.get("source_manifest_canonical_content_sha256"), field_name=f"frozen_snapshot.{timeframe}.canonical_content_sha256"),
        )
    datasets = tuple(
        _dataset_from_document(
            bundle_id=bundle_id,
            timeframe=timeframe,
            path=used_documents[timeframe][0],
            source_file_hash=used_documents[timeframe][2],
            canonical_content_hash=used_documents[timeframe][3],
            document=used_documents[timeframe][1],
        )
        for timeframe in COMPLETE_HISTORY_REQUIRED_TIMEFRAMES
    )
    _validate_coverage_map(coverage_map, bundle_cutoff_utc=shared_cutoff, datasets=datasets)
    instrument = manifest.get("instrument")
    if not isinstance(instrument, Mapping) or instrument.get("symbol") != "NASDAQ:GOOGL" or instrument.get("provider") != "twelve_data" or instrument.get("session") != "regular" or instrument.get("price_basis") != "split_adjusted_dividend_unadjusted_ohlcv":
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.SOURCE_METADATA_INCOMPATIBLE, "Bundle instrument metadata is not the approved NASDAQ:GOOGL regular split-adjusted source.")
    return CompleteHistoryBundle.create(
        bundle_id=bundle_id,
        bundle_directory=str(directory),
        shared_cutoff_utc=shared_cutoff,
        manifest_file_sha256=manifest_file_hash,
        manifest_canonical_content_sha256=manifest_canonical,
        coverage_map_content_sha256=_hash(files["coverage_map"].get("canonical_content_sha256"), field_name="coverage_map_hash"),
        identity_bridge_content_sha256=_hash(files["identity_bridge"].get("canonical_content_sha256"), field_name="identity_bridge_hash"),
        datasets=datasets,
        warnings=(
            "Native 4h history begins at the immutable coverage-map boundary; earlier branches remain not_covered.",
            "The runner is shadow-only and no provider was called while loading this bundle.",
        ),
    )


def _stage_plans(policy: CompleteHistoryShadowPolicy) -> tuple[CompleteHistoryStagePlan, ...]:
    return (
        CompleteHistoryStagePlan(
            stage=CompleteHistoryShadowStage.MONTHLY_ROOT,
            target_timeframe="monthly",
            parent_slots_per_hypothesis=1,
            reserved_model_calls_per_hypothesis=1,
            coverage_behavior="Both isolated Monthly maps must span the same approved IPO-to-as-of source window, connect every top-level segment, and retain exactly one terminal active segment; only a bounded explicit IPO origin control is permitted.",
        ),
        CompleteHistoryStagePlan(
            stage=CompleteHistoryShadowStage.WEEKLY_CHILDREN,
            target_timeframe="weekly",
            parent_slots_per_hypothesis=policy.maximum_monthly_map_segments - 1,
            reserved_model_calls_per_hypothesis=1,
            coverage_behavior="One role-isolated Weekly batch covers every completed Monthly map segment with exact-price typed boundary lineage and full native Weekly coverage; missing coverage remains not_covered rather than blocking the Monthly map.",
        ),
    )


def build_complete_history_shadow_plan(
    bundle: CompleteHistoryBundle,
    *,
    policy: CompleteHistoryShadowPolicy | None = None,
    created_at_utc: str | None = None,
) -> CompleteHistoryShadowPlan:
    """Return a provider-free, deterministic model-call reservation plan."""

    if not isinstance(bundle, CompleteHistoryBundle):
        raise TypeError("bundle must be a CompleteHistoryBundle created by load_complete_history_bundle().")
    if bundle.content_hash != complete_history_bundle_content_hash(bundle):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID, "Complete-history bundle object hash is invalid.")
    policy = policy or complete_history_shadow_policy()
    if policy.content_hash != complete_history_shadow_policy_content_hash(policy):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID, "Complete-history policy hash is invalid.")
    timestamp = _utc(created_at_utc or bundle.shared_cutoff_utc, field_name="created_at_utc")
    stages = _stage_plans(policy)
    hypotheses = tuple(
        CompleteHistoryHypothesisPlan(
            role=role,
            maximum_nodes=policy.maximum_nodes_per_hypothesis,
            reserved_model_calls=policy.maximum_model_calls_per_hypothesis,
            stages=stages,
        )
        for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)
    )
    return CompleteHistoryShadowPlan.create(
        plan_id="",
        bundle_content_hash=bundle.content_hash,
        policy=policy,
        hypotheses=hypotheses,
        total_reserved_model_calls=policy.maximum_model_calls_per_hypothesis * len(hypotheses),
        requires_human_approval=True,
        warnings=(
            "Stage 1 reserves at most four model calls: one Monthly map and one optional batched Weekly graph request per isolated hypothesis.",
            "A Weekly batch is omitted when its Monthly map contains no completed segments with complete native Weekly coverage; no unused reservation is spent.",
            "The active-parent Monthly-map Stage-1 contract is versioned: prior root/weekly artifacts cannot resume because their plan, approval, and artifact schemas predate the connected map and batched Weekly lineage rules.",
            "Stage 1 has exactly four possible calls: two isolated Monthly maps and up to one role-isolated Weekly batch for each map.",
            "BlindCandidateRulesPack v1.0 is static, symbol-independent, and content-hashed into every candidate packet, approval, plan, and artifact lineage.",
            "A model call cannot start without a separately supplied human approval bound to this exact plan hash.",
        ),
        created_at_utc=timestamp,
    )


def _coverage_attestation(
    *,
    dataset: CompleteHistoryDataset,
    window: NativeOHLCVWindow,
    start_pivot: TypedPivot,
    end_pivot: TypedPivot,
) -> ProofCoverageAttestation:
    return ProofCoverageAttestation.create(
        attestation_id="",
        proof_timeframe=dataset.timeframe,
        dataset_id=window.dataset_cutoff.dataset_id,
        source_window_hash=window.content_hash,
        interval_start_utc=start_pivot.timestamp_utc,
        interval_end_utc=end_pivot.timestamp_utc,
        coverage_complete=True,
        missing_interval_ids=(),
        coverage_method="immutable_bundle_window_bracketing",
    )


def _not_covered_attestation(
    *,
    dataset: CompleteHistoryDataset,
    start_pivot: TypedPivot,
    end_pivot: TypedPivot,
    reason: str,
) -> ProofCoverageAttestation:
    return ProofCoverageAttestation.create(
        attestation_id="",
        proof_timeframe=dataset.timeframe,
        dataset_id=dataset.proof_dataset_cutoff.dataset_id,
        source_window_hash=dataset.source_window.content_hash,
        interval_start_utc=start_pivot.timestamp_utc,
        interval_end_utc=end_pivot.timestamp_utc,
        coverage_complete=False,
        missing_interval_ids=(reason,),
        coverage_method="immutable_bundle_declared_missing_coverage",
    )


def _focused_window(
    *,
    dataset: CompleteHistoryDataset,
    start_pivot: TypedPivot,
    end_pivot: TypedPivot,
    window_id_prefix: str,
) -> tuple[NativeOHLCVWindow | None, ProofCoverageAttestation]:
    """Return a bracketing native window or an explicit not-covered fact."""

    start = _utc_datetime(start_pivot.timestamp_utc)
    end = _utc_datetime(end_pivot.timestamp_utc)
    if start < _utc_datetime(dataset.coverage_start_utc) or end > _utc_datetime(dataset.coverage_end_utc):
        reason = f"{dataset.timeframe}_coverage_{dataset.coverage_start_utc}_through_{dataset.coverage_end_utc}"
        return None, _not_covered_attestation(dataset=dataset, start_pivot=start_pivot, end_pivot=end_pivot, reason=reason)
    rows = dataset.source_window.candles
    first_index = next((index for index, candle in enumerate(rows) if _utc_datetime(candle.timestamp_utc) <= start and (index == len(rows) - 1 or _utc_datetime(rows[index + 1].timestamp_utc) > start)), None)
    last_index = next((index for index, candle in enumerate(rows) if _utc_datetime(candle.timestamp_utc) >= end), None)
    if first_index is None or last_index is None or first_index > last_index:
        reason = f"{dataset.timeframe}_no_complete_bracketing_interval"
        return None, _not_covered_attestation(dataset=dataset, start_pivot=start_pivot, end_pivot=end_pivot, reason=reason)
    selected = rows[first_index:last_index + 1]
    try:
        window = NativeOHLCVWindow.create(
            window_id=f"{window_id_prefix}:{dataset.timeframe}:{first_index}:{last_index}",
            dataset_cutoff=dataset.proof_dataset_cutoff,
            timeframe=dataset.timeframe,
            candles=selected,
            coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
            is_native=True,
        )
    except (TypeError, ValueError) as exc:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.DATASET_COVERAGE_INVALID, f"Could not isolate a native {dataset.timeframe} proof window.") from exc
    return window, _coverage_attestation(dataset=dataset, window=window, start_pivot=start_pivot, end_pivot=end_pivot)


_IPO_ORIGIN_CONTROL_MAXIMUM_MONTHLY_BARS = 2


def _lineage_reference(value: ProofBoundaryLineage) -> dict[str, str]:
    return {
        "lineage_id": value.lineage_id,
        "parent_pivot_id": value.parent_pivot.pivot_id,
        "child_pivot_id": value.child_pivot.pivot_id,
        "mapping_kind": value.mapping_kind,
    }


def _source_row_index(window: NativeOHLCVWindow, pivot: TypedPivot) -> int:
    for index, candle in enumerate(window.candles):
        if candle.source_row_hash == pivot.source_bar_hash and candle.timestamp_utc == pivot.timestamp_utc:
            return index
    raise CompleteHistoryShadowError(
        CompleteHistoryShadowReasonCode.PIVOT_LINEAGE_UNAVAILABLE,
        "A parent pivot does not identify a source row in its immutable native window.",
    )


def _build_parent_boundary_lineage(
    *,
    parent_wave: ProofWaveSnapshot,
    parent_window: NativeOHLCVWindow,
    child_window: NativeOHLCVWindow,
    child_catalog: Sequence[TypedPivot],
    analysis_cutoff_utc: str,
) -> tuple[ProofBoundaryLineage, ProofBoundaryLineage]:
    """Map root boundaries to exact-price lower-timeframe pivots.

    A lower-timeframe pivot may have a different candle timestamp, but it must
    fall inside the source candle represented by the parent pivot.  This keeps
    session/date bracketing explicit while prohibiting price substitution.
    """

    cutoff = _utc_datetime(analysis_cutoff_utc)
    rows = parent_window.candles
    mappings: list[ProofBoundaryLineage] = []
    for parent in (parent_wave.start_pivot, parent_wave.end_pivot):
        parent_index = _source_row_index(parent_window, parent)
        interval_start = _utc_datetime(rows[parent_index].timestamp_utc)
        interval_end = (
            _utc_datetime(rows[parent_index + 1].timestamp_utc)
            if parent_index + 1 < len(rows)
            else cutoff
        )
        if interval_end <= interval_start:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.PIVOT_LINEAGE_UNAVAILABLE,
                "The parent source-candle boundary cannot bracket a lower-timeframe pivot.",
            )
        candidates = [
            item
            for item in child_catalog
            if item.price_field == parent.price_field
            and abs(item.price - parent.price) <= _EPSILON
            and interval_start <= _utc_datetime(item.timestamp_utc) < interval_end
        ]
        if not candidates:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.PIVOT_LINEAGE_UNAVAILABLE,
                "No exact-price lower-timeframe typed pivot is available inside the required parent source candle.",
                reason_codes=(CompleteHistoryStage1HardRuleCode.PARENT_CHILD_LINEAGE_MISMATCH.value,),
            )
        child = min(candidates, key=lambda item: (item.timestamp_utc, item.pivot_id))
        mappings.append(
            ProofBoundaryLineage.create(
                lineage_id="",
                parent_pivot=parent,
                child_pivot=child,
                mapping_kind="exact_price_source_candle_bracket",
            )
        )
    if mappings[0].child_pivot.timestamp_utc >= mappings[1].child_pivot.timestamp_utc:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.PIVOT_LINEAGE_UNAVAILABLE,
            "The deterministic lower-timeframe boundary lineage is not chronologically ordered.",
            reason_codes=(CompleteHistoryStage1HardRuleCode.PARENT_CHILD_LINEAGE_MISMATCH.value,),
        )
    return tuple(mappings)  # type: ignore[return-value]


def screen_complete_history_stage1_root(
    root: CompleteHistoryRootCandidate,
) -> CompleteHistoryStage1ScreenResult:
    reasons: list[CompleteHistoryStage1HardRuleCode] = []
    if root.wave.end_pivot.timestamp_utc != root.analysis_window_end_utc:
        reasons.append(CompleteHistoryStage1HardRuleCode.ROOT_WINDOW_NOT_FULL)
    if root.origin_control_start_pivot is None:
        if root.wave.start_pivot.timestamp_utc != root.analysis_window_start_utc:
            reasons.append(CompleteHistoryStage1HardRuleCode.ROOT_WINDOW_NOT_FULL)
    elif (
        root.origin_control_start_pivot.timestamp_utc != root.analysis_window_start_utc
        or root.origin_control_end_pivot is None
        or root.origin_control_end_pivot.content_hash != root.wave.start_pivot.content_hash
    ):
        reasons.append(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID)
    return CompleteHistoryStage1ScreenResult.create(
        scope="monthly_root",
        passed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def screen_complete_history_stage1_child_graph(
    request: CompleteHistoryStageRequest,
    graph: CandidateChildGraph,
) -> CompleteHistoryStage1ScreenResult:
    """Run hard eligibility checks before any Daily candidate call can occur."""

    reasons: list[CompleteHistoryStage1HardRuleCode] = []
    positions = tuple(item.sequence_position for item in graph.children)
    expected = expected_child_positions(graph.declared_family, positions)
    if expected is None or positions != expected:
        reasons.append(CompleteHistoryStage1HardRuleCode.CHILD_SEQUENCE_INCOMPLETE)
    for previous, current in zip(graph.children, graph.children[1:]):
        if previous.end_pivot.pivot_id != current.start_pivot.pivot_id:
            reasons.append(CompleteHistoryStage1HardRuleCode.CHILD_BOUNDARY_DISCONNECTED)
            break
    if graph.children:
        expected_lineages = {
            (item.parent_pivot.pivot_id, item.child_pivot.pivot_id)
            for item in request.parent_boundary_lineage
        }
        observed_lineages = {
            (request.parent_wave.start_pivot.pivot_id, graph.children[0].start_pivot.pivot_id),
            (request.parent_wave.end_pivot.pivot_id, graph.children[-1].end_pivot.pivot_id),
        }
        if observed_lineages != expected_lineages:
            reasons.append(CompleteHistoryStage1HardRuleCode.PARENT_CHILD_LINEAGE_MISMATCH)
    else:
        reasons.append(CompleteHistoryStage1HardRuleCode.CHILD_SEQUENCE_INCOMPLETE)
    if graph.declared_family == "impulse" and set(positions) == {"1", "2", "3", "4", "5"}:
        children = {item.sequence_position: item for item in graph.children}
        wave_one_end = children["1"].end_pivot.price
        wave_four_end = children["4"].end_pivot.price
        if (
            request.parent_wave.direction == "up" and wave_four_end <= wave_one_end
        ) or (
            request.parent_wave.direction == "down" and wave_four_end >= wave_one_end
        ):
            reasons.append(CompleteHistoryStage1HardRuleCode.STANDARD_IMPULSE_WAVE4_OVERLAP)
    return CompleteHistoryStage1ScreenResult.create(
        scope="weekly_child_graph" if request.stage is CompleteHistoryShadowStage.WEEKLY_CHILDREN else "deeper_child_graph",
        passed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _require_stage1_screen(screen: CompleteHistoryStage1ScreenResult) -> None:
    if screen.passed:
        return
    raise CompleteHistoryShadowError(
        CompleteHistoryShadowReasonCode.STAGE1_HARD_RULE_FAILED,
        "Stage-1 deterministic scope and hard-rule screening rejected the candidate graph.",
        reason_codes=tuple(item.value for item in screen.reason_codes),
    )


def _root_count_signature(root: CompleteHistoryRootCandidate) -> str:
    return canonical_sha256(
        {
            "wave": {
                "degree": root.wave.degree,
                "declared_family": root.wave.declared_family,
                "direction": root.wave.direction,
                "completion_state": root.wave.completion_state,
                "start_pivot": root.wave.start_pivot.pivot_id,
                "end_pivot": root.wave.end_pivot.pivot_id,
                "invalidation": root.wave.invalidation.to_dict(),
            },
            "origin_control": (
                None
                if root.origin_control_start_pivot is None
                else {
                    "start": root.origin_control_start_pivot.pivot_id,
                    "end": root.origin_control_end_pivot.pivot_id if root.origin_control_end_pivot is not None else None,
                }
            ),
        }
    )


def _validate_stage1_root_pair(
    primary: CompleteHistoryRootCandidate,
    alternative: CompleteHistoryRootCandidate,
) -> CompleteHistoryStage1ScreenResult:
    reasons: list[CompleteHistoryStage1HardRuleCode] = []
    if (
        primary.analysis_window_start_utc != alternative.analysis_window_start_utc
        or primary.analysis_window_end_utc != alternative.analysis_window_end_utc
        or (
            primary.origin_control_start_pivot or primary.wave.start_pivot
        ).content_hash
        != (
            alternative.origin_control_start_pivot or alternative.wave.start_pivot
        ).content_hash
        or primary.wave.end_pivot.content_hash != alternative.wave.end_pivot.content_hash
    ):
        reasons.append(CompleteHistoryStage1HardRuleCode.ROOT_PAIR_WINDOW_MISMATCH)
    if _root_count_signature(primary) == _root_count_signature(alternative):
        reasons.append(CompleteHistoryStage1HardRuleCode.ROOT_PAIR_NOT_DISTINCT)
    return CompleteHistoryStage1ScreenResult.create(
        scope="primary_alternative_monthly_window",
        passed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _material_pivot_signature(pivot: TypedPivot) -> Mapping[str, Any]:
    return {
        "timestamp_utc": pivot.timestamp_utc,
        "price": pivot.price,
        "price_field": pivot.price_field.value,
        "pivot_type": pivot.pivot_type.value,
        "source_bar_hash": pivot.source_bar_hash,
    }


def _material_wave_signature(wave: ProofWaveSnapshot) -> Mapping[str, Any]:
    return {
        "degree": wave.degree,
        "declared_family": wave.declared_family,
        "direction": wave.direction,
        "completion_state": wave.completion_state,
        "start_pivot": _material_pivot_signature(wave.start_pivot),
        "end_pivot": _material_pivot_signature(wave.end_pivot),
        "invalidation": {
            "threshold_price": wave.invalidation.threshold_price,
            "direction": wave.invalidation.direction,
            "evaluation_basis": wave.invalidation.evaluation_basis,
            "source_pivot_id": wave.invalidation.source_pivot_id,
        },
    }


def screen_complete_history_monthly_map(
    monthly_map: CompleteHistoryMonthlyMap,
    *,
    monthly_window: NativeOHLCVWindow,
    maximum_segments: int,
) -> CompleteHistoryStage1ScreenResult:
    """Screen connected source-bar coverage before any Weekly call is allowed."""

    reasons: list[CompleteHistoryStage1HardRuleCode] = []
    if monthly_window.timeframe != "monthly" or not monthly_window.is_native:
        reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_AS_OF_INVALID)
    if len(monthly_map.segments) > maximum_segments:
        reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_GAP)
    if monthly_map.as_of_monthly_candle.timestamp_utc != monthly_window.candles[-1].timestamp_utc:
        reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_AS_OF_INVALID)
    if monthly_map.as_of_monthly_candle.source_row_hash != monthly_window.candles[-1].source_row_hash:
        reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_AS_OF_INVALID)
    try:
        expected_start_index = 0
        if monthly_map.origin_control_start_pivot is not None:
            origin_start_index = _source_row_index(monthly_window, monthly_map.origin_control_start_pivot)
            origin_end_index = _source_row_index(monthly_window, monthly_map.origin_control_end_pivot)
            if (
                origin_start_index != 0
                or origin_end_index <= origin_start_index
                or origin_end_index >= _IPO_ORIGIN_CONTROL_MAXIMUM_MONTHLY_BARS
            ):
                reasons.append(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID)
            expected_start_index = origin_end_index
        for index, segment in enumerate(monthly_map.segments):
            start_index = _source_row_index(monthly_window, segment.start_pivot)
            end_index = _source_row_index(monthly_window, segment.end_pivot)
            if start_index != expected_start_index:
                reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_GAP)
            if index < len(monthly_map.segments) - 1:
                if segment.completion_state != "completed" or end_index <= start_index:
                    reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_DISCONNECTED)
                expected_start_index = end_index
            else:
                if (
                    segment.completion_state != "active"
                    or end_index < start_index
                    or end_index > len(monthly_window.candles) - 1
                ):
                    reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_TERMINAL_ACTIVE_INVALID)
        if monthly_map.outer_parent.start_pivot.content_hash != monthly_map.segments[0].start_pivot.content_hash:
            reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_DISCONNECTED)
        if monthly_map.outer_parent.end_pivot.content_hash != monthly_map.active_final_segment.end_pivot.content_hash:
            reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_TERMINAL_ACTIVE_INVALID)
    except CompleteHistoryShadowError:
        reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_DISCONNECTED)
    return CompleteHistoryStage1ScreenResult.create(
        scope="monthly_map",
        passed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _monthly_map_signature(monthly_map: CompleteHistoryMonthlyMap) -> str:
    return canonical_sha256(
        {
            "analysis_window_start_utc": monthly_map.analysis_window_start_utc,
            "analysis_window_end_utc": monthly_map.analysis_window_end_utc,
            "as_of_monthly_candle": {
                "timestamp_utc": monthly_map.as_of_monthly_candle.timestamp_utc,
                "source_row_hash": monthly_map.as_of_monthly_candle.source_row_hash,
            },
            "outer_parent": _material_wave_signature(monthly_map.outer_parent),
            "segments": [_material_wave_signature(item) for item in monthly_map.segments],
            "origin_control": (
                None
                if monthly_map.origin_control_start_pivot is None
                else {
                    "start": _material_pivot_signature(monthly_map.origin_control_start_pivot),
                    "end": _material_pivot_signature(monthly_map.origin_control_end_pivot),
                }
            ),
        }
    )


def _validate_monthly_map_pair(
    primary: CompleteHistoryMonthlyMap,
    alternative: CompleteHistoryMonthlyMap,
) -> CompleteHistoryStage1ScreenResult:
    reasons: list[CompleteHistoryStage1HardRuleCode] = []
    if (
        primary.analysis_window_start_utc != alternative.analysis_window_start_utc
        or primary.analysis_window_end_utc != alternative.analysis_window_end_utc
        or primary.as_of_monthly_candle.source_row_hash != alternative.as_of_monthly_candle.source_row_hash
    ):
        reasons.append(CompleteHistoryStage1HardRuleCode.ROOT_PAIR_WINDOW_MISMATCH)
    if _monthly_map_signature(primary) == _monthly_map_signature(alternative):
        reasons.append(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_PAIR_NOT_DISTINCT)
    return CompleteHistoryStage1ScreenResult.create(
        scope="primary_alternative_monthly_map",
        passed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _root_packet(
    *,
    bundle: CompleteHistoryBundle,
    plan: CompleteHistoryShadowPlan,
    role: RecursiveProofHypothesisRole,
    monthly: CompleteHistoryDataset,
    pivot_catalog: Sequence[TypedPivot],
) -> Mapping[str, Any]:
    return _freeze_json(
        {
            "packet_kind": "blind_complete_history_monthly_root_candidate",
            "bundle_content_hash": bundle.content_hash,
            "plan_content_hash": plan.content_hash,
            "rules_pack_content_hash": plan.policy.blind_candidate_rules_pack.content_hash,
            "blind_candidate_rules_pack": plan.policy.blind_candidate_rules_pack.to_dict(),
            "role": role.value,
            "analysis_cutoff_utc": bundle.shared_cutoff_utc,
            "proof_ladder": COMPLETE_HISTORY_PROOF_LADDER,
            "monthly_window": monthly.source_window.to_dict(),
            "pivot_catalog": [item.to_dict() for item in pivot_catalog],
            "approved_root_scope": {
                "analysis_window_start_utc": monthly.source_window.candles[0].timestamp_utc,
                "analysis_window_end_utc": monthly.source_window.candles[-1].timestamp_utc,
                "requires_full_monthly_window": True,
                "origin_control": {
                    "allowed_only_at_ipo": True,
                    "maximum_monthly_source_bars": _IPO_ORIGIN_CONTROL_MAXIMUM_MONTHLY_BARS,
                },
            },
            "constraints": {
                "blind": True,
                "candidate_only": True,
                "forbidden_fields": ("verified", "verification_status", "confidence", "report_prose"),
                "no_alternative_input": True,
                "no_news_or_fundamental_input": True,
                "rules_pack_content_hash": plan.policy.blind_candidate_rules_pack.content_hash,
            },
        }
    )


def _monthly_map_packet(
    *,
    bundle: CompleteHistoryBundle,
    plan: CompleteHistoryShadowPlan,
    role: RecursiveProofHypothesisRole,
    monthly: CompleteHistoryDataset,
    pivot_catalog: Sequence[TypedPivot],
) -> Mapping[str, Any]:
    """Build the blind, full-history Monthly-map input for one isolated role."""

    as_of = monthly.source_window.candles[-1]
    return _freeze_json(
        {
            "packet_kind": "blind_complete_history_monthly_map",
            "bundle_content_hash": bundle.content_hash,
            "plan_content_hash": plan.content_hash,
            "rules_pack_content_hash": plan.policy.blind_candidate_rules_pack.content_hash,
            "blind_candidate_rules_pack": plan.policy.blind_candidate_rules_pack.to_dict(),
            "role": role.value,
            "analysis_cutoff_utc": bundle.shared_cutoff_utc,
            "proof_ladder": COMPLETE_HISTORY_PROOF_LADDER,
            "monthly_window": monthly.source_window.to_dict(),
            "pivot_catalog": [item.to_dict() for item in pivot_catalog],
            "as_of_monthly_candle": as_of.to_dict(),
            "approved_monthly_map_scope": {
                "analysis_window_start_utc": monthly.source_window.candles[0].timestamp_utc,
                "analysis_window_end_utc": as_of.timestamp_utc,
                "requires_full_monthly_window": True,
                "requires_connected_top_level_segments": True,
                "requires_exactly_one_terminal_active_segment": True,
                "maximum_segments": plan.policy.maximum_monthly_map_segments,
                "origin_control": {
                    "allowed_only_at_ipo": True,
                    "maximum_monthly_source_bars": _IPO_ORIGIN_CONTROL_MAXIMUM_MONTHLY_BARS,
                },
            },
            "constraints": {
                "blind": True,
                "candidate_only": True,
                "forbidden_fields": ("verified", "verification_status", "confidence", "report_prose"),
                "no_alternative_input": True,
                "no_news_or_fundamental_input": True,
                "rules_pack_content_hash": plan.policy.blind_candidate_rules_pack.content_hash,
            },
        }
    )


def _stage_packet(request: CompleteHistoryStageRequest) -> Mapping[str, Any]:
    return _freeze_json(
        {
            "packet_kind": "blind_complete_history_child_graph",
            "request_id": request.request_id,
            "request_content_hash": request.content_hash,
            "rules_pack_content_hash": request.blind_candidate_rules_pack.content_hash,
            "blind_candidate_rules_pack": request.blind_candidate_rules_pack.to_dict(),
            "role": request.role.value,
            "analysis_cutoff_utc": request.analysis_cutoff_utc,
            "parent_wave": request.parent_wave.to_dict(),
            "target_child_degree": request.target_child_degree,
            "target_timeframe": request.target_timeframe,
            "native_window": request.native_window.to_dict(),
            "pivot_catalog": [item.to_dict() for item in request.pivot_catalog],
            "boundary_lineage": [_lineage_reference(item) for item in request.parent_boundary_lineage],
            "constraints": {
                "blind": True,
                "candidate_only": True,
                "forbidden_fields": ("verified", "verification_status", "confidence", "report_prose"),
                "no_alternative_input": True,
                "no_news_or_fundamental_input": True,
                "rules_pack_content_hash": request.blind_candidate_rules_pack.content_hash,
            },
        }
    )


def _weekly_batch_packet(request: CompleteHistoryWeeklyBatchRequest) -> Mapping[str, Any]:
    """Serialize a batched Weekly request without exposing non-Weekly data."""

    segments: list[dict[str, Any]] = []
    for item in request.segment_requests:
        stage_packet = _stage_packet(item)
        segments.append(
            {
                "segment_id": item.parent_wave.wave_id,
                "request_id": item.request_id,
                "request_content_hash": item.content_hash,
                "parent_wave": stage_packet["parent_wave"],
                "target_child_degree": item.target_child_degree,
                "target_timeframe": item.target_timeframe,
                "native_window": stage_packet["native_window"],
                "pivot_catalog": stage_packet["pivot_catalog"],
                "boundary_lineage": stage_packet["boundary_lineage"],
            }
        )
    return _freeze_json(
        {
            "packet_kind": "blind_complete_history_weekly_child_graph_batch",
            "batch_id": request.batch_id,
            "batch_content_hash": request.content_hash,
            "monthly_map_content_hash": request.monthly_map_content_hash,
            "rules_pack_content_hash": request.blind_candidate_rules_pack.content_hash,
            "blind_candidate_rules_pack": request.blind_candidate_rules_pack.to_dict(),
            "role": request.role.value,
            "analysis_cutoff_utc": request.analysis_cutoff_utc,
            "target_timeframe": "weekly",
            "segment_requests": segments,
            "constraints": {
                "blind": True,
                "candidate_only": True,
                "forbidden_fields": ("verified", "verification_status", "confidence", "report_prose"),
                "no_alternative_input": True,
                "no_news_or_fundamental_input": True,
                "rules_pack_content_hash": request.blind_candidate_rules_pack.content_hash,
            },
        }
    )


def _require_provider(provider: CompleteHistoryCandidateProvider) -> tuple[str, str | None]:
    root_method = getattr(provider, "generate_monthly_map", None)
    graph_method = getattr(provider, "generate_weekly_child_graph_batch", None)
    name = getattr(provider, "name", None)
    model = getattr(provider, "model", None)
    if not callable(root_method) or not callable(graph_method) or not isinstance(name, str) or not name.strip() or (model is not None and (not isinstance(model, str) or not model.strip())):
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.PROVIDER_INTERFACE_INVALID,
            "Provider must expose named Monthly-map and batched Weekly graph methods with a non-empty name.",
        )
    return name.strip(), model.strip() if isinstance(model, str) else None


def _parse_root_candidate(
    raw: Mapping[str, Any],
    *,
    role: RecursiveProofHypothesisRole,
    packet_hash: str,
    monthly_window: NativeOHLCVWindow,
    pivot_catalog: Sequence[TypedPivot],
    provider: str,
    model: str | None,
    generated_at_utc: str,
) -> CompleteHistoryRootCandidate:
    if not isinstance(raw, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, "Monthly root output must be an object.")
    value = dict(raw)
    allowed = {
        "candidate_id",
        "degree",
        "declared_family",
        "direction",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "origin_control",
        "invalidation",
    }
    try:
        _reject_unknown(value, allowed, model_name="MonthlyRootCandidate")
        missing = allowed - set(value)
        if missing:
            raise ValueError("Monthly root output is missing: " + ", ".join(sorted(missing)) + ".")
        catalog = {item.pivot_id: item for item in pivot_catalog}
        start = catalog[_text(value["start_pivot_id"], field_name="start_pivot_id")]
        end = catalog[_text(value["end_pivot_id"], field_name="end_pivot_id")]
        analysis_start_utc = monthly_window.candles[0].timestamp_utc
        analysis_end_utc = monthly_window.candles[-1].timestamp_utc
        origin_raw = value["origin_control"]
        origin_start: TypedPivot | None = None
        origin_end: TypedPivot | None = None
        if origin_raw is None:
            if start.timestamp_utc != analysis_start_utc:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                    "Monthly root omitted the IPO boundary without an explicit origin control interval.",
                    reason_codes=(CompleteHistoryStage1HardRuleCode.ROOT_WINDOW_NOT_FULL.value,),
                )
        else:
            if not isinstance(origin_raw, Mapping):
                raise ValueError("origin_control must be null or an object.")
            origin_value = dict(origin_raw)
            origin_fields = {"start_pivot_id", "end_pivot_id"}
            _reject_unknown(origin_value, origin_fields, model_name="MonthlyRootOriginControl")
            if set(origin_value) != origin_fields:
                raise ValueError("origin_control must contain exact start/end pivot IDs.")
            origin_start = catalog[_text(origin_value["start_pivot_id"], field_name="origin_control.start_pivot_id")]
            origin_end = catalog[_text(origin_value["end_pivot_id"], field_name="origin_control.end_pivot_id")]
            if (
                origin_start.timestamp_utc != analysis_start_utc
                or origin_end.content_hash != start.content_hash
            ):
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                    "Origin control must connect the first Monthly source candle directly to the root start pivot.",
                    reason_codes=(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID.value,),
                )
            origin_end_index = _source_row_index(monthly_window, origin_end)
            if origin_end_index >= _IPO_ORIGIN_CONTROL_MAXIMUM_MONTHLY_BARS:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                    "Origin control may cover only the narrowly bounded IPO source interval.",
                    reason_codes=(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID.value,),
                )
        if end.timestamp_utc != analysis_end_utc:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                "Monthly root does not reach the final completed Monthly period of the approved analysis window.",
                reason_codes=(CompleteHistoryStage1HardRuleCode.ROOT_WINDOW_NOT_FULL.value,),
            )
        degree = _text(value["degree"], field_name="degree")
        if degree not in STANDARD_DEGREES or degree == "Unassigned":
            raise ValueError("Monthly root degree is unsupported.")
        _terminal_degree(degree)
        invalidation = CandidateInvalidation.from_dict(value["invalidation"])
        if invalidation.source_pivot_id not in catalog:
            raise ValueError("Monthly root invalidation source is outside the immutable pivot catalog.")
        wave = ProofWaveSnapshot.create(
            wave_id=_text(value["candidate_id"], field_name="candidate_id"),
            degree=degree,
            declared_family=_text(value["declared_family"], field_name="declared_family"),
            direction=_text(value["direction"], field_name="direction"),
            completion_state=_text(value["completion_state"], field_name="completion_state"),
            start_pivot=start,
            end_pivot=end,
            invalidation=invalidation,
            source_reference_hash=packet_hash,
            source_kind="complete_history_monthly_root_candidate",
        )
        if start.source_window_hash != monthly_window.native_rows_hash or end.source_window_hash != monthly_window.native_rows_hash:
            raise ValueError("Monthly root pivots do not retain monthly source-window lineage.")
        result = CompleteHistoryRootCandidate.create(
            candidate_id="",
            role=role,
            wave=wave,
            analysis_window_start_utc=analysis_start_utc,
            analysis_window_end_utc=analysis_end_utc,
            origin_control_start_pivot=origin_start,
            origin_control_end_pivot=origin_end,
            source_packet_hash=packet_hash,
            provider=provider,
            model=model,
            generated_at_utc=generated_at_utc,
        )
        _require_stage1_screen(screen_complete_history_stage1_root(result))
        return result
    except CompleteHistoryShadowError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, "Monthly root candidate is not a valid blind candidate-only graph root.") from exc


def _parse_monthly_map(
    raw: Mapping[str, Any],
    *,
    role: RecursiveProofHypothesisRole,
    packet_hash: str,
    monthly_window: NativeOHLCVWindow,
    pivot_catalog: Sequence[TypedPivot],
    provider: str,
    model: str | None,
    generated_at_utc: str,
    maximum_segments: int,
) -> CompleteHistoryMonthlyMap:
    """Normalize one blind full-history Monthly map; never verify it here."""

    if not isinstance(raw, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, "Monthly map output must be an object.")
    value = dict(raw)
    allowed = {"map_id", "outer_parent", "origin_control", "as_of_monthly_candle_id", "segments"}
    wave_fields = {
        "degree",
        "declared_family",
        "direction",
        "completion_state",
        "start_pivot_id",
        "end_pivot_id",
        "invalidation",
    }
    parent_fields = {"parent_id", *wave_fields}
    segment_fields = {"segment_id", *wave_fields}
    origin_fields = {"start_pivot_id", "end_pivot_id"}
    try:
        _reject_unknown(value, allowed, model_name="CompleteHistoryMonthlyMap")
        if set(value) != allowed:
            raise ValueError("Monthly map output must contain its exact strict fields.")
        catalog = {item.pivot_id: item for item in pivot_catalog}
        if len(catalog) != len(pivot_catalog):
            raise ValueError("Monthly pivot catalog contains duplicate IDs.")
        as_of = monthly_window.candles[-1]
        if _text(value["as_of_monthly_candle_id"], field_name="as_of_monthly_candle_id") != as_of.candle_id:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                "Monthly map as-of candle must exactly match the final immutable Monthly source candle.",
                reason_codes=(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_AS_OF_INVALID.value,),
            )

        origin_raw = value["origin_control"]
        origin_start: TypedPivot | None = None
        origin_end: TypedPivot | None = None
        if origin_raw is not None:
            if not isinstance(origin_raw, Mapping):
                raise ValueError("origin_control must be null or an object.")
            origin = dict(origin_raw)
            _reject_unknown(origin, origin_fields, model_name="CompleteHistoryMonthlyMapOriginControl")
            if set(origin) != origin_fields:
                raise ValueError("origin_control requires exact start/end pivot IDs.")
            origin_start = catalog[_text(origin["start_pivot_id"], field_name="origin_control.start_pivot_id")]
            origin_end = catalog[_text(origin["end_pivot_id"], field_name="origin_control.end_pivot_id")]
            if (
                origin_start.timestamp_utc != monthly_window.candles[0].timestamp_utc
                or _source_row_index(monthly_window, origin_end) >= _IPO_ORIGIN_CONTROL_MAXIMUM_MONTHLY_BARS
            ):
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                    "Monthly origin control must remain within the bounded IPO source interval.",
                    reason_codes=(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID.value,),
                )

        outer_raw = value["outer_parent"]
        if not isinstance(outer_raw, Mapping):
            raise ValueError("outer_parent must be an object.")
        outer_value = dict(outer_raw)
        _reject_unknown(outer_value, parent_fields, model_name="CompleteHistoryMonthlyMapOuterParent")
        if set(outer_value) != parent_fields:
            raise ValueError("outer_parent requires exact strict fields.")
        outer_start = catalog[_text(outer_value["start_pivot_id"], field_name="outer_parent.start_pivot_id")]
        outer_end = catalog[_text(outer_value["end_pivot_id"], field_name="outer_parent.end_pivot_id")]
        outer_invalidation = CandidateInvalidation.from_dict(outer_value["invalidation"])
        if outer_invalidation.source_pivot_id not in catalog:
            raise ValueError("Outer parent invalidation source is outside the immutable Monthly pivot catalog.")
        outer_parent = ProofWaveSnapshot.create(
            wave_id=_text(outer_value["parent_id"], field_name="outer_parent.parent_id"),
            degree=_text(outer_value["degree"], field_name="outer_parent.degree"),
            declared_family=_text(outer_value["declared_family"], field_name="outer_parent.declared_family"),
            direction=_text(outer_value["direction"], field_name="outer_parent.direction"),
            completion_state=_text(outer_value["completion_state"], field_name="outer_parent.completion_state"),
            start_pivot=outer_start,
            end_pivot=outer_end,
            invalidation=outer_invalidation,
            source_reference_hash=packet_hash,
            source_kind="complete_history_monthly_map_outer_parent",
        )
        if outer_parent.completion_state != "active":
            raise ValueError("Monthly map outer parent must remain active.")

        raw_segments = value["segments"]
        if isinstance(raw_segments, (str, bytes)) or not isinstance(raw_segments, Sequence):
            raise ValueError("Monthly map segments must be a sequence.")
        if not raw_segments or len(raw_segments) > maximum_segments:
            raise ValueError("Monthly map segment count exceeds the approved bounded policy.")
        segments: list[ProofWaveSnapshot] = []
        for index, raw_segment in enumerate(raw_segments):
            if not isinstance(raw_segment, Mapping):
                raise ValueError("Monthly map segment entries must be objects.")
            segment_value = dict(raw_segment)
            _reject_unknown(segment_value, segment_fields, model_name=f"CompleteHistoryMonthlyMapSegment[{index}]")
            if set(segment_value) != segment_fields:
                raise ValueError("Monthly map segment entries require exact strict fields.")
            start = catalog[_text(segment_value["start_pivot_id"], field_name="segment.start_pivot_id")]
            end = catalog[_text(segment_value["end_pivot_id"], field_name="segment.end_pivot_id")]
            invalidation = CandidateInvalidation.from_dict(segment_value["invalidation"])
            if invalidation.source_pivot_id not in catalog:
                raise ValueError("Monthly map segment invalidation source is outside the immutable pivot catalog.")
            segments.append(
                ProofWaveSnapshot.create(
                    wave_id=_text(segment_value["segment_id"], field_name="segment.segment_id"),
                    degree=_text(segment_value["degree"], field_name="segment.degree"),
                    declared_family=_text(segment_value["declared_family"], field_name="segment.declared_family"),
                    direction=_text(segment_value["direction"], field_name="segment.direction"),
                    completion_state=_text(segment_value["completion_state"], field_name="segment.completion_state"),
                    start_pivot=start,
                    end_pivot=end,
                    invalidation=invalidation,
                    source_reference_hash=packet_hash,
                    source_kind="complete_history_monthly_map_segment",
                )
            )

        result = CompleteHistoryMonthlyMap.create(
            map_id=_text(value["map_id"], field_name="map_id"),
            role=role,
            outer_parent=outer_parent,
            segments=tuple(segments),
            as_of_monthly_candle=as_of,
            analysis_window_start_utc=monthly_window.candles[0].timestamp_utc,
            analysis_window_end_utc=as_of.timestamp_utc,
            origin_control_start_pivot=origin_start,
            origin_control_end_pivot=origin_end,
            source_packet_hash=packet_hash,
            provider=provider,
            model=model,
            generated_at_utc=generated_at_utc,
        )
        screen = screen_complete_history_monthly_map(
            result,
            monthly_window=monthly_window,
            maximum_segments=maximum_segments,
        )
        if not screen.passed:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                "Monthly map omitted, disconnected, or misclassified part of the immutable full-history window.",
                reason_codes=tuple(item.value for item in screen.reason_codes),
            )
        return result
    except CompleteHistoryShadowError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
            "Monthly map output cannot become a valid blind candidate-only full-history map.",
        ) from exc


def _parse_child_graph(
    raw: Mapping[str, Any],
    *,
    request: CompleteHistoryStageRequest,
    provider: str,
    model: str | None,
    terminal_degree: str,
) -> CandidateChildGraph:
    if not isinstance(raw, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, "Child graph output must be an object.")
    value = dict(raw)
    allowed = {"graph_id", "declared_family", "boundary_lineage", "children"}
    child_allowed = {"child_id", "sequence_position", "direction", "declared_family", "start_pivot_id", "end_pivot_id", "invalidation"}
    try:
        _reject_unknown(value, allowed, model_name="CompleteHistoryChildGraph")
        missing = allowed - set(value)
        if missing:
            raise ValueError("Child graph output is missing: " + ", ".join(sorted(missing)) + ".")
        family = _text(value["declared_family"], field_name="declared_family").lower()
        if family != request.parent_wave.declared_family:
            raise ValueError("A direct child graph must use the parent candidate family; a model cannot silently relabel the parent.")
        raw_lineage = value["boundary_lineage"]
        if isinstance(raw_lineage, (str, bytes)) or not isinstance(raw_lineage, Sequence):
            raise ValueError("boundary_lineage must be a sequence.")
        expected_lineage = tuple(_lineage_reference(item) for item in request.parent_boundary_lineage)
        normalized_lineage: list[dict[str, str]] = []
        lineage_fields = {"lineage_id", "parent_pivot_id", "child_pivot_id", "mapping_kind"}
        for index, raw_item in enumerate(raw_lineage):
            if not isinstance(raw_item, Mapping):
                raise ValueError("boundary_lineage entries must be objects.")
            item = dict(raw_item)
            _reject_unknown(item, lineage_fields, model_name=f"CompleteHistoryBoundaryLineage[{index}]")
            if set(item) != lineage_fields:
                raise ValueError("boundary_lineage entries must contain exact lineage fields.")
            normalized_lineage.append(
                {
                    "lineage_id": _text(item["lineage_id"], field_name="lineage_id"),
                    "parent_pivot_id": _text(item["parent_pivot_id"], field_name="parent_pivot_id"),
                    "child_pivot_id": _text(item["child_pivot_id"], field_name="child_pivot_id"),
                    "mapping_kind": _text(item["mapping_kind"], field_name="mapping_kind"),
                }
            )
        if tuple(normalized_lineage) != expected_lineage:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.STAGE1_HARD_RULE_FAILED,
                "Child graph boundary lineage does not exactly match the immutable parent/child mapping packet.",
                reason_codes=(CompleteHistoryStage1HardRuleCode.PARENT_CHILD_LINEAGE_MISMATCH.value,),
            )
        raw_children = value["children"]
        if isinstance(raw_children, (str, bytes)) or not isinstance(raw_children, Sequence):
            raise ValueError("children must be a sequence.")
        catalog = {item.pivot_id: item for item in request.pivot_catalog}
        children: list[GeneratedChildWave] = []
        for index, raw_child in enumerate(raw_children):
            if not isinstance(raw_child, Mapping):
                raise ValueError("Child graph entries must be objects.")
            child = dict(raw_child)
            _reject_unknown(child, child_allowed, model_name=f"CompleteHistoryChild[{index}]")
            missing_child = child_allowed - set(child)
            if missing_child:
                raise ValueError("Child graph entry is missing: " + ", ".join(sorted(missing_child)) + ".")
            start = catalog[_text(child["start_pivot_id"], field_name="start_pivot_id")]
            end = catalog[_text(child["end_pivot_id"], field_name="end_pivot_id")]
            invalidation = CandidateInvalidation.from_dict(child["invalidation"])
            if invalidation.source_pivot_id not in catalog:
                raise ValueError("Child invalidation source is outside the immutable pivot catalog.")
            children.append(
                GeneratedChildWave(
                    child_id=_text(child["child_id"], field_name="child_id"),
                    parent_candidate_id=request.parent_wave.wave_id,
                    degree=request.target_child_degree,
                    timeframe=request.target_timeframe,
                    sequence_position=_text(child["sequence_position"], field_name="sequence_position"),
                    direction=_text(child["direction"], field_name="direction"),
                    declared_family=_text(child["declared_family"], field_name="declared_family"),
                    start_pivot=start,
                    end_pivot=end,
                    invalidation=invalidation,
                )
            )
        return CandidateChildGraph.create(
            graph_id=_text(value["graph_id"], field_name="graph_id"),
            role=CandidateGraphRole(request.role.value),
            request_id=request.request_id,
            request_content_hash=request.content_hash,
            parent_candidate_id=request.parent_wave.wave_id,
            target_child_degree=request.target_child_degree,
            target_timeframe=request.target_timeframe,
            declared_family=family,
            children=tuple(children),
            proof_scope=CandidateProofScope(
                target_timeframe=request.target_timeframe,
                verified_to_timeframe=None,
                terminal_degree=terminal_degree,
                coverage_limitations=(),
            ),
            provider=provider,
            model=model,
            generated_at_utc=request.created_at_utc,
            policy_version=COMPLETE_HISTORY_SHADOW_POLICY_VERSION,
        )
    except CompleteHistoryShadowError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, "Child output cannot become a valid candidate-only graph.") from exc


def _parse_weekly_child_graph_batch(
    raw: Mapping[str, Any],
    *,
    request: CompleteHistoryWeeklyBatchRequest,
    provider: str,
    model: str | None,
) -> CompleteHistoryWeeklyChildGraphBatch:
    """Normalize exactly one Weekly graph per eligible completed Monthly segment."""

    if not isinstance(raw, Mapping):
        raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID, "Weekly graph batch output must be an object.")
    value = dict(raw)
    allowed = {"batch_id", "graphs"}
    entry_fields = {"segment_id", "graph"}
    try:
        _reject_unknown(value, allowed, model_name="CompleteHistoryWeeklyChildGraphBatch")
        if set(value) != allowed:
            raise ValueError("Weekly graph batch output must contain exact strict fields.")
        raw_graphs = value["graphs"]
        if isinstance(raw_graphs, (str, bytes)) or not isinstance(raw_graphs, Sequence):
            raise ValueError("Weekly graph batch graphs must be a sequence.")
        requests_by_segment = {item.parent_wave.wave_id: item for item in request.segment_requests}
        graph_inputs: dict[str, Mapping[str, Any]] = {}
        for index, raw_entry in enumerate(raw_graphs):
            if not isinstance(raw_entry, Mapping):
                raise ValueError("Weekly graph batch entries must be objects.")
            entry = dict(raw_entry)
            _reject_unknown(entry, entry_fields, model_name=f"CompleteHistoryWeeklyChildGraphBatch[{index}]")
            if set(entry) != entry_fields:
                raise ValueError("Weekly graph batch entries require exact segment_id and graph fields.")
            segment_id = _text(entry["segment_id"], field_name="segment_id")
            graph = entry["graph"]
            if segment_id in graph_inputs or segment_id not in requests_by_segment or not isinstance(graph, Mapping):
                raise ValueError("Weekly graph batch must contain exactly one object graph for each requested Monthly segment.")
            graph_inputs[segment_id] = graph
        if tuple(graph_inputs) != tuple(requests_by_segment):
            raise ValueError("Weekly graph batch did not return every eligible Monthly segment in deterministic request order.")
        graphs = tuple(
            _parse_child_graph(
                graph_inputs[item.parent_wave.wave_id],
                request=item,
                provider=provider,
                model=model,
                terminal_degree=_terminal_degree(item.parent_wave.degree),
            )
            for item in request.segment_requests
        )
        result = CompleteHistoryWeeklyChildGraphBatch.create(
            batch_id=_text(value["batch_id"], field_name="batch_id"),
            role=request.role,
            batch_request_content_hash=request.content_hash,
            graphs=graphs,
            provider=provider,
            model=model,
            generated_at_utc=request.created_at_utc,
        )
        if tuple(item.parent_candidate_id for item in result.graphs) != tuple(requests_by_segment):
            raise ValueError("Normalized Weekly graphs do not preserve the request segment order.")
        return result
    except CompleteHistoryShadowError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CompleteHistoryShadowError(
            CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
            "Weekly graph batch cannot become valid candidate-only graph evidence.",
        ) from exc


def _shadow_source_identifiers(bundle: CompleteHistoryBundle) -> tuple[int, int, str, str]:
    """Create non-persistent positive IDs required by the existing proof contract.

    These are deterministic compatibility surrogates only.  They never point
    at or claim to be a stored analysis-run or degree-resolution row.
    """

    analysis_hash = canonical_sha256({"bundle": bundle.content_hash, "kind": "complete_history_shadow_analysis_reference"})
    resolution_hash = canonical_sha256({"bundle": bundle.content_hash, "kind": "complete_history_shadow_resolution_reference"})
    analysis_id = (int(analysis_hash[:15], 16) % 2_000_000_000) + 1
    resolution_id = (int(resolution_hash[:15], 16) % 2_000_000_000) + 1
    return analysis_id, resolution_id, analysis_hash, resolution_hash


class CompleteHistoryShadowRunner:
    """Manual coordinator; planning is read-only and execution is caller-gated."""

    def preflight(self, bundle_directory: str | Path, *, created_at_utc: str | None = None) -> tuple[CompleteHistoryBundle, CompleteHistoryShadowPlan]:
        bundle = load_complete_history_bundle(bundle_directory)
        return bundle, build_complete_history_shadow_plan(bundle, created_at_utc=created_at_utc)

    def _execute_legacy_single_root(
        self,
        bundle: CompleteHistoryBundle,
        plan: CompleteHistoryShadowPlan,
        *,
        provider: CompleteHistoryCandidateProvider,
        approval: CompleteHistoryHumanApproval | None,
        allow_model_calls: bool = False,
        created_at_utc: str | None = None,
        output_directory: str | Path | None = None,
        resume_artifacts: bool = True,
    ) -> CompleteHistoryShadowExecution:
        """Run one stage-scoped shadow execution.

        Tests may supply a fake provider.  The module never creates a live
        provider, so a real caller must explicitly construct one and supply a
        matching human approval plus ``allow_model_calls=True``.  When an
        output directory is supplied, each accepted structured stage result is
        saved as an immutable content-addressed JSON artifact and may be
        resumed only through matching lineage.
        """

        if bundle.content_hash != complete_history_bundle_content_hash(bundle):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID, "Complete-history bundle hash is invalid.")
        if plan.content_hash != complete_history_shadow_plan_content_hash(plan) or plan.bundle_content_hash != bundle.content_hash:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID, "Complete-history plan does not match the verified bundle.")
        if not allow_model_calls:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_CALL_NOT_AUTHORIZED, "allow_model_calls=True is required for every provider call.")
        if approval is None:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.HUMAN_APPROVAL_REQUIRED, "A separately created human approval bound to this plan is required.")
        if approval.content_hash != complete_history_human_approval_content_hash(approval) or approval.plan_content_hash != plan.content_hash:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.APPROVAL_PLAN_MISMATCH, "Human approval does not match this exact immutable call plan.")
        if approval.rules_pack_content_hash != plan.policy.blind_candidate_rules_pack.content_hash:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_RULES_PACK_MISMATCH,
                "Human approval does not bind the exact blind candidate rules pack in this plan.",
            )
        expected_call_limit = complete_history_stage_call_limit(plan, approval.max_stage)
        if approval.exact_call_limit != expected_call_limit:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_CALL_LIMIT_MISMATCH,
                f"Approval exact_call_limit must be {expected_call_limit} through {approval.max_stage.value}.",
            )
        if not isinstance(resume_artifacts, bool):
            raise TypeError("resume_artifacts must be boolean.")
        provider_name, provider_model = _require_provider(provider)
        if approval.provider != provider_name or approval.model != provider_model:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_PROVIDER_MODEL_MISMATCH,
                "Human approval provider/model does not match the supplied candidate provider.",
            )
        timestamp = _utc(created_at_utc or approval.approved_at_utc, field_name="created_at_utc")
        artifact_store = (
            CompleteHistoryLocalArtifactStore(output_directory, bundle_directory=bundle.bundle_directory)
            if output_directory is not None
            else None
        )
        created_artifact_hashes: list[str] = []
        reused_artifact_hashes: list[str] = []
        # The approval ceiling is applied to transport attempts, not accepted
        # candidates. A malformed or rejected response consumes its one slot.
        attempted_calls = {role: 0 for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)}
        accepted_calls = {role: 0 for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)}
        rejected_calls = {role: 0 for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)}
        nodes = {role: 0 for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)}
        roots: list[CompleteHistoryRootCandidate] = []
        bundles: list[RecursiveProofBundle] = []
        monthly = bundle.dataset("monthly")
        # The runner exposes a source-limited pivot catalog, not an automatic
        # wave detector.  A one-bar catalog preserves every local native high
        # and low for the candidate provider; deterministic proof later checks
        # the chosen pivots and hard rules independently.
        monthly_catalog = build_native_pivot_catalog(monthly.source_window, pivot_window=1)
        proof_policy = monthly_to_4h_recursive_proof_policy()
        analysis_id, resolution_id, analysis_hash, resolution_hash = _shadow_source_identifiers(bundle)

        def find_artifact(
            *,
            stage: CompleteHistoryMaximumStage,
            role: RecursiveProofHypothesisRole,
            stage_input_hash: str,
            lineage_parent_artifact_hash: str | None,
        ) -> CompleteHistoryStageArtifact | None:
            if artifact_store is None or not resume_artifacts:
                return None
            artifact = artifact_store.find(
                stage=stage,
                role=role,
                plan_content_hash=plan.content_hash,
                approval_content_hash=approval.content_hash,
                bundle_content_hash=bundle.content_hash,
                max_stage=approval.max_stage,
                exact_call_limit=approval.exact_call_limit,
                stage_input_hash=stage_input_hash,
                lineage_parent_artifact_hash=lineage_parent_artifact_hash,
            )
            if artifact is not None:
                if artifact.rules_pack_content_hash != plan.policy.blind_candidate_rules_pack.content_hash:
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed artifact does not bind the plan's blind candidate rules pack.",
                    )
                reused_artifact_hashes.append(artifact.content_hash)
            return artifact

        def persist_artifact(
            *,
            stage: CompleteHistoryMaximumStage,
            role: RecursiveProofHypothesisRole,
            stage_input_hash: str,
            lineage_parent_artifact_hash: str | None,
            output_kind: CompleteHistoryStageArtifactOutputKind,
            structured_output: Mapping[str, Any],
            normalized_output_content_hash: str,
        ) -> CompleteHistoryStageArtifact | None:
            if artifact_store is None:
                return None
            artifact = CompleteHistoryStageArtifact.create(
                artifact_id="",
                stage=stage,
                role=role,
                plan_content_hash=plan.content_hash,
                approval_content_hash=approval.content_hash,
                bundle_content_hash=bundle.content_hash,
                max_stage=approval.max_stage,
                exact_call_limit=approval.exact_call_limit,
                stage_input_hash=stage_input_hash,
                lineage_parent_artifact_hash=lineage_parent_artifact_hash,
                output_kind=output_kind,
                structured_output=structured_output,
                structured_output_hash=canonical_sha256(structured_output),
                normalized_output_content_hash=normalized_output_content_hash,
                created_at_utc=timestamp,
                rules_pack_content_hash=plan.policy.blind_candidate_rules_pack.content_hash,
            )
            artifact_store.write(artifact)
            created_artifact_hashes.append(artifact.content_hash)
            return artifact

        def reserve_call(role: RecursiveProofHypothesisRole) -> None:
            if attempted_calls[role] >= plan.policy.maximum_model_calls_per_hypothesis:
                raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED, f"{role.value} reached its strict {plan.policy.maximum_model_calls_per_hypothesis}-call reservation.")
            if sum(attempted_calls.values()) >= approval.exact_call_limit:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED,
                    f"The approved {approval.max_stage.value} scope reached its exact {approval.exact_call_limit}-call limit.",
                )
            attempted_calls[role] += 1

        def provider_failure(error: Exception) -> CompleteHistoryShadowError:
            attempted = sum(attempted_calls.values())
            accepted = sum(accepted_calls.values())
            rejected = sum(rejected_calls.values())
            if isinstance(error, CompleteHistoryShadowError):
                return CompleteHistoryShadowError(
                    error.code,
                    error.detail,
                    attempted_provider_calls=attempted,
                    accepted_provider_calls=accepted,
                    rejected_provider_calls=rejected,
                    reason_codes=error.reason_codes,
                )
            return CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
                "Candidate provider call failed before a valid structured candidate was accepted.",
                attempted_provider_calls=attempted,
                accepted_provider_calls=accepted,
                rejected_provider_calls=rejected,
            )

        def reserve_node(role: RecursiveProofHypothesisRole) -> None:
            if nodes[role] >= plan.policy.maximum_nodes_per_hypothesis:
                raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED, f"{role.value} reached its strict {plan.policy.maximum_nodes_per_hypothesis}-node reservation.")
            nodes[role] += 1

        def make_node(
            *,
            role: RecursiveProofHypothesisRole,
            wave: ProofWaveSnapshot,
            own_dataset: CompleteHistoryDataset,
            own_window: NativeOHLCVWindow,
            own_catalog: tuple[TypedPivot, ...],
            level: int,
            source_artifact_hash: str | None,
        ) -> tuple[RecursiveProofNode, tuple[RecursiveProofNode, ...]]:
            reserve_node(role)
            own_coverage = _coverage_attestation(dataset=own_dataset, window=own_window, start_pivot=wave.start_pivot, end_pivot=wave.end_pivot)
            if level == len(COMPLETE_HISTORY_PROOF_LADDER) - 1:
                node = RecursiveProofNode.create(
                    # The recursive verifier intentionally requires direct
                    # child_node_ids to equal the candidate graph child IDs.
                    # Provider child IDs are therefore the immutable node IDs
                    # at every non-root level; duplicate IDs are rejected by
                    # RecursiveProofBundle rather than rewritten here.
                    node_id=wave.wave_id,
                    wave=wave,
                    proof_timeframe=own_dataset.timeframe,
                    native_window=own_window,
                    native_pivot_catalog=own_catalog,
                    coverage=own_coverage,
                )
                return node, (node,)
            target_timeframe = COMPLETE_HISTORY_PROOF_LADDER[level + 1]
            if not _stage_is_authorized(
                target_timeframe=target_timeframe,
                max_stage=approval.max_stage,
            ):
                node = RecursiveProofNode.create(
                    node_id=wave.wave_id,
                    wave=wave,
                    proof_timeframe=own_dataset.timeframe,
                    native_window=own_window,
                    native_pivot_catalog=own_catalog,
                    coverage=own_coverage,
                )
                return node, (node,)
            target_dataset = bundle.dataset(target_timeframe)
            target_window, child_coverage = _focused_window(
                dataset=target_dataset,
                start_pivot=wave.start_pivot,
                end_pivot=wave.end_pivot,
                window_id_prefix=f"complete_history:{bundle.bundle_id}:{role.value}:{wave.wave_id}",
            )
            if target_window is None:
                node = RecursiveProofNode.create(
                    node_id=wave.wave_id,
                    wave=wave,
                    proof_timeframe=own_dataset.timeframe,
                    native_window=own_window,
                    native_pivot_catalog=own_catalog,
                    coverage=own_coverage,
                    child_coverage=child_coverage,
                )
                return node, (node,)
            target_catalog = build_native_pivot_catalog(target_window, pivot_window=1)
            boundary_lineage = _build_parent_boundary_lineage(
                parent_wave=wave,
                parent_window=own_window,
                child_window=target_window,
                child_catalog=target_catalog,
                analysis_cutoff_utc=bundle.shared_cutoff_utc,
            )
            request = CompleteHistoryStageRequest.create(
                request_id="",
                role=role,
                parent_wave=wave,
                stage=(
                    CompleteHistoryShadowStage.WEEKLY_CHILDREN
                    if target_timeframe == "weekly"
                    else CompleteHistoryShadowStage.DAILY_CHILDREN
                    if target_timeframe == "daily"
                    else CompleteHistoryShadowStage.FOUR_HOUR_CHILDREN
                ),
                target_child_degree=_next_degree(wave.degree),
                target_timeframe=target_timeframe,
                native_window=target_window,
                pivot_catalog=target_catalog,
                parent_boundary_lineage=boundary_lineage,
                plan_content_hash=plan.content_hash,
                policy_content_hash=plan.policy.content_hash,
                analysis_cutoff_utc=bundle.shared_cutoff_utc,
                created_at_utc=timestamp,
                blind_candidate_rules_pack=plan.policy.blind_candidate_rules_pack,
            )
            stage_scope = _MAXIMUM_STAGE_BY_TIMEFRAME[target_timeframe]
            artifact = find_artifact(
                stage=stage_scope,
                role=role,
                stage_input_hash=request.content_hash,
                lineage_parent_artifact_hash=source_artifact_hash,
            )
            if artifact is not None:
                if (
                    artifact.output_kind is not CompleteHistoryStageArtifactOutputKind.CHILD_GRAPH
                    or artifact.created_at_utc != timestamp
                ):
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed child-graph artifact has incompatible output metadata.",
                    )
                graph = _parse_child_graph(
                    artifact.structured_output,
                    request=request,
                    provider=provider_name,
                    model=provider_model,
                    terminal_degree=_terminal_degree(roots_by_role[role].wave.degree),
                )
                if graph.content_hash != artifact.normalized_output_content_hash:
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed child-graph artifact does not reproduce its normalized graph hash.",
                    )
                graph_artifact_hash = artifact.content_hash
            else:
                reserve_call(role)
                try:
                    raw_graph = provider.generate_child_graph(
                        role=role,
                        packet=_stage_packet(request),
                        output_schema=CHILD_GRAPH_OUTPUT_SCHEMA,
                    )
                    graph = _parse_child_graph(
                        raw_graph,
                        request=request,
                        provider=provider_name,
                        model=provider_model,
                        terminal_degree=_terminal_degree(roots_by_role[role].wave.degree),
                    )
                except Exception as exc:
                    rejected_calls[role] += 1
                    raise provider_failure(exc) from exc
                accepted_calls[role] += 1
                artifact = persist_artifact(
                    stage=stage_scope,
                    role=role,
                    stage_input_hash=request.content_hash,
                    lineage_parent_artifact_hash=source_artifact_hash,
                    output_kind=CompleteHistoryStageArtifactOutputKind.CHILD_GRAPH,
                    structured_output=raw_graph,
                    normalized_output_content_hash=graph.content_hash,
                )
                graph_artifact_hash = artifact.content_hash if artifact is not None else None
            if request.stage is CompleteHistoryShadowStage.WEEKLY_CHILDREN:
                _require_stage1_screen(screen_complete_history_stage1_child_graph(request, graph))
            child_nodes: list[RecursiveProofNode] = []
            all_nodes: list[RecursiveProofNode] = []
            for child in graph.children:
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
                    source_kind="complete_history_candidate_child_graph",
                )
                child_node, descendants = make_node(
                    role=role,
                    wave=child_wave,
                    own_dataset=target_dataset,
                    own_window=target_window,
                    own_catalog=target_catalog,
                    level=level + 1,
                    source_artifact_hash=graph_artifact_hash,
                )
                child_nodes.append(child_node)
                all_nodes.extend(descendants)
            node = RecursiveProofNode.create(
                node_id=wave.wave_id,
                wave=wave,
                proof_timeframe=own_dataset.timeframe,
                native_window=own_window,
                native_pivot_catalog=own_catalog,
                coverage=own_coverage,
                child_graph=graph,
                child_window=target_window,
                child_pivot_catalog=target_catalog,
                child_boundary_lineage=request.parent_boundary_lineage,
                child_coverage=child_coverage,
                child_node_ids=tuple(item.node_id for item in child_nodes),
            )
            return node, (*all_nodes, node)

        roots_by_role: dict[RecursiveProofHypothesisRole, CompleteHistoryRootCandidate] = {}
        root_artifact_hashes: dict[RecursiveProofHypothesisRole, str | None] = {}
        for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE):
            root_packet = _root_packet(bundle=bundle, plan=plan, role=role, monthly=monthly, pivot_catalog=monthly_catalog)
            root_packet_hash = canonical_sha256(root_packet)
            artifact = find_artifact(
                stage=CompleteHistoryMaximumStage.MONTHLY,
                role=role,
                stage_input_hash=root_packet_hash,
                lineage_parent_artifact_hash=None,
            )
            if artifact is not None:
                if (
                    artifact.output_kind is not CompleteHistoryStageArtifactOutputKind.ROOT_CANDIDATE
                    or artifact.created_at_utc != timestamp
                ):
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed root artifact has incompatible output metadata.",
                    )
                root = _parse_root_candidate(
                    artifact.structured_output,
                    role=role,
                    packet_hash=root_packet_hash,
                    monthly_window=monthly.source_window,
                    pivot_catalog=monthly_catalog,
                    provider=provider_name,
                    model=provider_model,
                    generated_at_utc=timestamp,
                )
                if root.content_hash != artifact.normalized_output_content_hash:
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed root artifact does not reproduce its normalized candidate hash.",
                    )
                root_artifact_hashes[role] = artifact.content_hash
            else:
                reserve_call(role)
                try:
                    raw_root = provider.generate_monthly_root_candidate(
                        role=role,
                        packet=root_packet,
                        output_schema=MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA,
                    )
                    root = _parse_root_candidate(
                        raw_root,
                        role=role,
                        packet_hash=root_packet_hash,
                        monthly_window=monthly.source_window,
                        pivot_catalog=monthly_catalog,
                        provider=provider_name,
                        model=provider_model,
                        generated_at_utc=timestamp,
                    )
                except Exception as exc:
                    rejected_calls[role] += 1
                    raise provider_failure(exc) from exc
                accepted_calls[role] += 1
                artifact = persist_artifact(
                    stage=CompleteHistoryMaximumStage.MONTHLY,
                    role=role,
                    stage_input_hash=root_packet_hash,
                    lineage_parent_artifact_hash=None,
                    output_kind=CompleteHistoryStageArtifactOutputKind.ROOT_CANDIDATE,
                    structured_output=raw_root,
                    normalized_output_content_hash=root.content_hash,
                )
                root_artifact_hashes[role] = artifact.content_hash if artifact is not None else None
            roots_by_role[role] = root
            roots.append(root)
        _require_stage1_screen(
            _validate_stage1_root_pair(
                roots_by_role[RecursiveProofHypothesisRole.PRIMARY],
                roots_by_role[RecursiveProofHypothesisRole.ALTERNATIVE],
            )
        )
        for role in (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE):
            root = roots_by_role[role]
            root_node, tree_nodes = make_node(
                role=role,
                wave=root.wave,
                own_dataset=monthly,
                own_window=monthly.source_window,
                own_catalog=monthly_catalog,
                level=0,
                source_artifact_hash=root_artifact_hashes[role],
            )
            bundles.append(
                RecursiveProofBundle.create(
                    bundle_id=f"complete_history_shadow:{bundle.bundle_id}:{role.value}:{root.content_hash[:16]}",
                    role=role,
                    source_analysis_run_id=analysis_id,
                    source_analysis_run_hash=analysis_hash,
                    source_degree_resolution_id=resolution_id,
                    source_degree_resolution_hash=resolution_hash,
                    analysis_cutoff_utc=bundle.shared_cutoff_utc,
                    root_node_id=root_node.node_id,
                    nodes=tree_nodes,
                    policy=proof_policy,
                    shadow_mode=True,
                    created_at_utc=timestamp,
                )
            )
        pair = RecursiveProofBundlePair.create(
            pair_id="",
            primary_bundle=next(item for item in bundles if item.role is RecursiveProofHypothesisRole.PRIMARY),
            alternative_bundle=next(item for item in bundles if item.role is RecursiveProofHypothesisRole.ALTERNATIVE),
            created_at_utc=timestamp,
        )
        proof_result = RecursiveMultiTimeframeProofVerifier().verify_pair(pair, shadow_mode=True)
        warnings = [
            "All verification statuses were produced by RecursiveMultiTimeframeProofVerifier, not the candidate provider.",
            "No SQLite, forecast, resolution, CLI, or active-pipeline mutation occurred.",
            f"Execution stopped at the human-approved {approval.max_stage.value} stage and cannot generate later stages.",
            "Provider-call accounting: "
            f"attempted={sum(attempted_calls.values())}, accepted={sum(accepted_calls.values())}, "
            f"rejected={sum(rejected_calls.values())}.",
        ]
        if artifact_store is None:
            warnings.append("No local artifact output_directory was supplied; this execution is in-memory only and cannot resume from local artifacts.")
        else:
            warnings.append("Only validated structured candidate outputs were written as content-addressed local artifacts; no hidden reasoning or credentials were persisted.")
        if any(result.status is SubdivisionVerificationStatus.NOT_COVERED for result in (proof_result.primary_result, proof_result.alternative_result)):
            warnings.append("At least one supplied branch is not_covered; unavailable intervals were retained instead of skipped.")
        return CompleteHistoryShadowExecution.create(
            execution_id="",
            plan_content_hash=plan.content_hash,
            bundle_content_hash=bundle.content_hash,
            approval_content_hash=approval.content_hash,
            provider=provider_name,
            model=provider_model,
            root_candidates=tuple(roots),
            proof_result=proof_result,
            calls_primary=attempted_calls[RecursiveProofHypothesisRole.PRIMARY],
            calls_alternative=attempted_calls[RecursiveProofHypothesisRole.ALTERNATIVE],
            warnings=tuple(warnings),
            created_at_utc=timestamp,
            max_stage=approval.max_stage,
            exact_call_limit=approval.exact_call_limit,
            artifact_output_directory=str(artifact_store.directory) if artifact_store is not None else None,
            created_artifact_content_hashes=tuple(created_artifact_hashes),
            reused_artifact_content_hashes=tuple(reused_artifact_hashes),
            attempted_provider_calls=sum(attempted_calls.values()),
            accepted_provider_calls=sum(accepted_calls.values()),
            rejected_provider_calls=sum(rejected_calls.values()),
        )

    def execute(
        self,
        bundle: CompleteHistoryBundle,
        plan: CompleteHistoryShadowPlan,
        *,
        provider: CompleteHistoryCandidateProvider,
        approval: CompleteHistoryHumanApproval | None,
        allow_model_calls: bool = False,
        created_at_utc: str | None = None,
        output_directory: str | Path | None = None,
        resume_artifacts: bool = True,
    ) -> CompleteHistoryShadowExecution:
        """Run the versioned active-parent Monthly-map Stage-1 workflow.

        The outer parent is necessarily active at the decision-time cutoff, so
        it is not itself proof-eligible.  Completed map segments are the only
        intervals that can receive the batched Weekly candidate request.  This
        method intentionally stops at Weekly; daily/4h batching needs a later,
        separately approved contract rather than an unsafe per-segment fallback.
        """

        if bundle.content_hash != complete_history_bundle_content_hash(bundle):
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID, "Complete-history bundle hash is invalid.")
        if plan.content_hash != complete_history_shadow_plan_content_hash(plan) or plan.bundle_content_hash != bundle.content_hash:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.CANONICAL_HASH_INVALID, "Complete-history plan does not match the verified bundle.")
        if (
            plan.schema_version != COMPLETE_HISTORY_PLAN_SCHEMA_VERSION
            or plan.policy.schema_version != COMPLETE_HISTORY_SHADOW_POLICY_VERSION
        ):
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                "The active-parent Monthly-map runner requires a fresh versioned Monthly-map plan; prior root plans cannot resume.",
            )
        if not allow_model_calls:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.MODEL_CALL_NOT_AUTHORIZED, "allow_model_calls=True is required for every provider call.")
        if approval is None:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.HUMAN_APPROVAL_REQUIRED, "A separately created human approval bound to this plan is required.")
        if approval.content_hash != complete_history_human_approval_content_hash(approval) or approval.plan_content_hash != plan.content_hash:
            raise CompleteHistoryShadowError(CompleteHistoryShadowReasonCode.APPROVAL_PLAN_MISMATCH, "Human approval does not match this exact immutable call plan.")
        if approval.schema_version != COMPLETE_HISTORY_APPROVAL_SCHEMA_VERSION:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_STAGE_SCOPE_MISMATCH,
                "The active-parent Monthly-map runner requires a fresh versioned approval; historical approvals cannot resume a new map plan.",
            )
        if approval.rules_pack_content_hash != plan.policy.blind_candidate_rules_pack.content_hash:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_RULES_PACK_MISMATCH,
                "Human approval does not bind the exact blind candidate rules pack in this plan.",
            )
        if _maximum_stage_index(approval.max_stage) > _maximum_stage_index(CompleteHistoryMaximumStage.WEEKLY):
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.ACTIVE_MONTHLY_MAP_STAGE_UNSUPPORTED,
                "This active-parent Monthly-map contract authorizes candidate generation through the batched Weekly stage only.",
            )
        expected_call_limit = complete_history_stage_call_limit(plan, approval.max_stage)
        if approval.exact_call_limit != expected_call_limit:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_CALL_LIMIT_MISMATCH,
                f"Approval exact_call_limit must be {expected_call_limit} through {approval.max_stage.value}.",
            )
        if not isinstance(resume_artifacts, bool):
            raise TypeError("resume_artifacts must be boolean.")
        provider_name, provider_model = _require_provider(provider)
        if approval.provider != provider_name or approval.model != provider_model:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.APPROVAL_PROVIDER_MODEL_MISMATCH,
                "Human approval provider/model does not match the supplied candidate provider.",
            )

        timestamp = _utc(created_at_utc or approval.approved_at_utc, field_name="created_at_utc")
        artifact_store = (
            CompleteHistoryLocalArtifactStore(output_directory, bundle_directory=bundle.bundle_directory)
            if output_directory is not None
            else None
        )
        created_artifact_hashes: list[str] = []
        reused_artifact_hashes: list[str] = []
        roles = (RecursiveProofHypothesisRole.PRIMARY, RecursiveProofHypothesisRole.ALTERNATIVE)
        attempted_calls = {role: 0 for role in roles}
        accepted_calls = {role: 0 for role in roles}
        rejected_calls = {role: 0 for role in roles}
        nodes = {role: 0 for role in roles}
        monthly = bundle.dataset("monthly")
        weekly = bundle.dataset("weekly")
        monthly_catalog = build_native_pivot_catalog(monthly.source_window, pivot_window=1)
        proof_policy = monthly_to_4h_recursive_proof_policy()
        analysis_id, resolution_id, analysis_hash, resolution_hash = _shadow_source_identifiers(bundle)

        def find_artifact(
            *,
            stage: CompleteHistoryMaximumStage,
            role: RecursiveProofHypothesisRole,
            stage_input_hash: str,
            lineage_parent_artifact_hash: str | None,
        ) -> CompleteHistoryStageArtifact | None:
            if artifact_store is None or not resume_artifacts:
                return None
            artifact = artifact_store.find(
                stage=stage,
                role=role,
                plan_content_hash=plan.content_hash,
                approval_content_hash=approval.content_hash,
                bundle_content_hash=bundle.content_hash,
                max_stage=approval.max_stage,
                exact_call_limit=approval.exact_call_limit,
                stage_input_hash=stage_input_hash,
                lineage_parent_artifact_hash=lineage_parent_artifact_hash,
            )
            if artifact is not None:
                if (
                    artifact.schema_version != COMPLETE_HISTORY_STAGE_ARTIFACT_SCHEMA_VERSION
                    or artifact.rules_pack_content_hash != plan.policy.blind_candidate_rules_pack.content_hash
                ):
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed artifact does not bind the active Monthly-map plan and rules pack.",
                    )
                reused_artifact_hashes.append(artifact.content_hash)
            return artifact

        def persist_artifact(
            *,
            stage: CompleteHistoryMaximumStage,
            role: RecursiveProofHypothesisRole,
            stage_input_hash: str,
            lineage_parent_artifact_hash: str | None,
            output_kind: CompleteHistoryStageArtifactOutputKind,
            structured_output: Mapping[str, Any],
            normalized_output_content_hash: str,
        ) -> CompleteHistoryStageArtifact | None:
            if artifact_store is None:
                return None
            artifact = CompleteHistoryStageArtifact.create(
                artifact_id="",
                stage=stage,
                role=role,
                plan_content_hash=plan.content_hash,
                approval_content_hash=approval.content_hash,
                bundle_content_hash=bundle.content_hash,
                max_stage=approval.max_stage,
                exact_call_limit=approval.exact_call_limit,
                stage_input_hash=stage_input_hash,
                lineage_parent_artifact_hash=lineage_parent_artifact_hash,
                output_kind=output_kind,
                structured_output=structured_output,
                structured_output_hash=canonical_sha256(structured_output),
                normalized_output_content_hash=normalized_output_content_hash,
                created_at_utc=timestamp,
                rules_pack_content_hash=plan.policy.blind_candidate_rules_pack.content_hash,
            )
            artifact_store.write(artifact)
            created_artifact_hashes.append(artifact.content_hash)
            return artifact

        def reserve_call(role: RecursiveProofHypothesisRole) -> None:
            if attempted_calls[role] >= plan.policy.maximum_model_calls_per_hypothesis:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED,
                    f"{role.value} reached its strict {plan.policy.maximum_model_calls_per_hypothesis}-call reservation.",
                )
            if sum(attempted_calls.values()) >= approval.exact_call_limit:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED,
                    f"The approved {approval.max_stage.value} scope reached its exact {approval.exact_call_limit}-call limit.",
                )
            attempted_calls[role] += 1

        def provider_failure(error: Exception) -> CompleteHistoryShadowError:
            attempted = sum(attempted_calls.values())
            accepted = sum(accepted_calls.values())
            rejected = sum(rejected_calls.values())
            if isinstance(error, CompleteHistoryShadowError):
                return CompleteHistoryShadowError(
                    error.code,
                    error.detail,
                    attempted_provider_calls=attempted,
                    accepted_provider_calls=accepted,
                    rejected_provider_calls=rejected,
                    reason_codes=error.reason_codes,
                )
            return CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID,
                "Candidate provider call failed before a valid structured candidate was accepted.",
                attempted_provider_calls=attempted,
                accepted_provider_calls=accepted,
                rejected_provider_calls=rejected,
            )

        def reserve_node(role: RecursiveProofHypothesisRole) -> None:
            if nodes[role] >= plan.policy.maximum_nodes_per_hypothesis:
                raise CompleteHistoryShadowError(
                    CompleteHistoryShadowReasonCode.HYPOTHESIS_BUDGET_EXHAUSTED,
                    f"{role.value} reached its strict {plan.policy.maximum_nodes_per_hypothesis}-node reservation.",
                )
            nodes[role] += 1

        maps_by_role: dict[RecursiveProofHypothesisRole, CompleteHistoryMonthlyMap] = {}
        map_artifact_hashes: dict[RecursiveProofHypothesisRole, str | None] = {}
        for role in roles:
            packet = _monthly_map_packet(
                bundle=bundle,
                plan=plan,
                role=role,
                monthly=monthly,
                pivot_catalog=monthly_catalog,
            )
            packet_hash = canonical_sha256(packet)
            artifact = find_artifact(
                stage=CompleteHistoryMaximumStage.MONTHLY,
                role=role,
                stage_input_hash=packet_hash,
                lineage_parent_artifact_hash=None,
            )
            if artifact is not None:
                if artifact.output_kind is not CompleteHistoryStageArtifactOutputKind.MONTHLY_MAP or artifact.created_at_utc != timestamp:
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed Monthly-map artifact has incompatible versioned output metadata.",
                    )
                monthly_map = _parse_monthly_map(
                    artifact.structured_output,
                    role=role,
                    packet_hash=packet_hash,
                    monthly_window=monthly.source_window,
                    pivot_catalog=monthly_catalog,
                    provider=provider_name,
                    model=provider_model,
                    generated_at_utc=timestamp,
                    maximum_segments=plan.policy.maximum_monthly_map_segments,
                )
                if monthly_map.content_hash != artifact.normalized_output_content_hash:
                    raise CompleteHistoryShadowError(
                        CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                        "Resumed Monthly-map artifact does not reproduce its normalized map hash.",
                    )
                map_artifact_hashes[role] = artifact.content_hash
            else:
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
                        packet_hash=packet_hash,
                        monthly_window=monthly.source_window,
                        pivot_catalog=monthly_catalog,
                        provider=provider_name,
                        model=provider_model,
                        generated_at_utc=timestamp,
                        maximum_segments=plan.policy.maximum_monthly_map_segments,
                    )
                except Exception as exc:
                    rejected_calls[role] += 1
                    raise provider_failure(exc) from exc
                accepted_calls[role] += 1
                artifact = persist_artifact(
                    stage=CompleteHistoryMaximumStage.MONTHLY,
                    role=role,
                    stage_input_hash=packet_hash,
                    lineage_parent_artifact_hash=None,
                    output_kind=CompleteHistoryStageArtifactOutputKind.MONTHLY_MAP,
                    structured_output=raw_map,
                    normalized_output_content_hash=monthly_map.content_hash,
                )
                map_artifact_hashes[role] = artifact.content_hash if artifact is not None else None
            maps_by_role[role] = monthly_map

        map_pair_screen = _validate_monthly_map_pair(
            maps_by_role[RecursiveProofHypothesisRole.PRIMARY],
            maps_by_role[RecursiveProofHypothesisRole.ALTERNATIVE],
        )
        if not map_pair_screen.passed:
            raise CompleteHistoryShadowError(
                CompleteHistoryShadowReasonCode.MONTHLY_MAP_PAIR_NOT_DISTINCT
                if CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_PAIR_NOT_DISTINCT in map_pair_screen.reason_codes
                else CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID,
                "Primary and Alternative Monthly maps must cover the same history while remaining materially different.",
                reason_codes=tuple(item.value for item in map_pair_screen.reason_codes),
            )

        weekly_requests_by_role: dict[RecursiveProofHypothesisRole, tuple[CompleteHistoryStageRequest, ...]] = {}
        weekly_coverage_by_role: dict[RecursiveProofHypothesisRole, dict[str, ProofCoverageAttestation]] = {}
        for role in roles:
            requests: list[CompleteHistoryStageRequest] = []
            coverage_by_segment: dict[str, ProofCoverageAttestation] = {}
            if approval.max_stage is CompleteHistoryMaximumStage.WEEKLY:
                for segment in maps_by_role[role].completed_segments:
                    target_window, coverage = _focused_window(
                        dataset=weekly,
                        start_pivot=segment.start_pivot,
                        end_pivot=segment.end_pivot,
                        window_id_prefix=f"complete_history:{bundle.bundle_id}:{role.value}:{segment.wave_id}",
                    )
                    coverage_by_segment[segment.wave_id] = coverage
                    if target_window is None:
                        continue
                    target_catalog = build_native_pivot_catalog(target_window, pivot_window=1)
                    boundary_lineage = _build_parent_boundary_lineage(
                        parent_wave=segment,
                        parent_window=monthly.source_window,
                        child_window=target_window,
                        child_catalog=target_catalog,
                        analysis_cutoff_utc=bundle.shared_cutoff_utc,
                    )
                    requests.append(
                        CompleteHistoryStageRequest.create(
                            request_id="",
                            role=role,
                            parent_wave=segment,
                            stage=CompleteHistoryShadowStage.WEEKLY_CHILDREN,
                            target_child_degree=_next_degree(segment.degree),
                            target_timeframe="weekly",
                            native_window=target_window,
                            pivot_catalog=target_catalog,
                            parent_boundary_lineage=boundary_lineage,
                            plan_content_hash=plan.content_hash,
                            policy_content_hash=plan.policy.content_hash,
                            analysis_cutoff_utc=bundle.shared_cutoff_utc,
                            created_at_utc=timestamp,
                            blind_candidate_rules_pack=plan.policy.blind_candidate_rules_pack,
                        )
                    )
            weekly_requests_by_role[role] = tuple(requests)
            weekly_coverage_by_role[role] = coverage_by_segment

        weekly_batches_by_role: dict[RecursiveProofHypothesisRole, CompleteHistoryWeeklyChildGraphBatch] = {}
        weekly_batch_artifact_hashes: dict[RecursiveProofHypothesisRole, str | None] = {role: None for role in roles}
        if approval.max_stage is CompleteHistoryMaximumStage.WEEKLY:
            for role in roles:
                requests = weekly_requests_by_role[role]
                if not requests:
                    continue
                batch_request = CompleteHistoryWeeklyBatchRequest.create(
                    batch_id="",
                    role=role,
                    monthly_map_content_hash=maps_by_role[role].content_hash,
                    segment_requests=requests,
                    plan_content_hash=plan.content_hash,
                    policy_content_hash=plan.policy.content_hash,
                    analysis_cutoff_utc=bundle.shared_cutoff_utc,
                    created_at_utc=timestamp,
                    blind_candidate_rules_pack=plan.policy.blind_candidate_rules_pack,
                )
                packet = _weekly_batch_packet(batch_request)
                artifact = find_artifact(
                    stage=CompleteHistoryMaximumStage.WEEKLY,
                    role=role,
                    stage_input_hash=canonical_sha256(packet),
                    lineage_parent_artifact_hash=map_artifact_hashes[role],
                )
                if artifact is not None:
                    if artifact.output_kind is not CompleteHistoryStageArtifactOutputKind.WEEKLY_CHILD_GRAPH_BATCH or artifact.created_at_utc != timestamp:
                        raise CompleteHistoryShadowError(
                            CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                            "Resumed Weekly batch artifact has incompatible versioned output metadata.",
                        )
                    batch = _parse_weekly_child_graph_batch(
                        artifact.structured_output,
                        request=batch_request,
                        provider=provider_name,
                        model=provider_model,
                    )
                    if batch.content_hash != artifact.normalized_output_content_hash:
                        raise CompleteHistoryShadowError(
                            CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID,
                            "Resumed Weekly batch artifact does not reproduce its normalized graph-batch hash.",
                        )
                    weekly_batch_artifact_hashes[role] = artifact.content_hash
                else:
                    reserve_call(role)
                    try:
                        raw_batch = provider.generate_weekly_child_graph_batch(
                            role=role,
                            packet=packet,
                            output_schema=WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA,
                        )
                        batch = _parse_weekly_child_graph_batch(
                            raw_batch,
                            request=batch_request,
                            provider=provider_name,
                            model=provider_model,
                        )
                        for graph, segment_request in zip(batch.graphs, batch_request.segment_requests):
                            _require_stage1_screen(screen_complete_history_stage1_child_graph(segment_request, graph))
                    except Exception as exc:
                        rejected_calls[role] += 1
                        raise provider_failure(exc) from exc
                    accepted_calls[role] += 1
                    artifact = persist_artifact(
                        stage=CompleteHistoryMaximumStage.WEEKLY,
                        role=role,
                        stage_input_hash=canonical_sha256(packet),
                        lineage_parent_artifact_hash=map_artifact_hashes[role],
                        output_kind=CompleteHistoryStageArtifactOutputKind.WEEKLY_CHILD_GRAPH_BATCH,
                        structured_output=raw_batch,
                        normalized_output_content_hash=batch.content_hash,
                    )
                    weekly_batch_artifact_hashes[role] = artifact.content_hash if artifact is not None else None
                weekly_batches_by_role[role] = batch

        def make_outer_bundle(role: RecursiveProofHypothesisRole, monthly_map: CompleteHistoryMonthlyMap) -> RecursiveProofBundle:
            reserve_node(role)
            node = RecursiveProofNode.create(
                node_id=monthly_map.outer_parent.wave_id,
                wave=monthly_map.outer_parent,
                proof_timeframe="monthly",
                native_window=monthly.source_window,
                native_pivot_catalog=monthly_catalog,
                coverage=_coverage_attestation(
                    dataset=monthly,
                    window=monthly.source_window,
                    start_pivot=monthly_map.outer_parent.start_pivot,
                    end_pivot=monthly_map.outer_parent.end_pivot,
                ),
            )
            return RecursiveProofBundle.create(
                bundle_id=f"complete_history_active_map_outer:{bundle.bundle_id}:{role.value}:{monthly_map.content_hash[:16]}",
                role=role,
                source_analysis_run_id=analysis_id,
                source_analysis_run_hash=analysis_hash,
                source_degree_resolution_id=resolution_id,
                source_degree_resolution_hash=resolution_hash,
                analysis_cutoff_utc=bundle.shared_cutoff_utc,
                root_node_id=node.node_id,
                nodes=(node,),
                policy=proof_policy,
                shadow_mode=True,
                created_at_utc=timestamp,
            )

        def make_segment_proof(
            role: RecursiveProofHypothesisRole,
            monthly_map: CompleteHistoryMonthlyMap,
            segment: ProofWaveSnapshot,
            graph: CandidateChildGraph | None,
            request: CompleteHistoryStageRequest | None,
        ) -> CompleteHistorySegmentProofResult:
            reserve_node(role)
            root_kwargs: dict[str, Any] = {
                "node_id": segment.wave_id,
                "wave": segment,
                "proof_timeframe": "monthly",
                "native_window": monthly.source_window,
                "native_pivot_catalog": monthly_catalog,
                "coverage": _coverage_attestation(
                    dataset=monthly,
                    window=monthly.source_window,
                    start_pivot=segment.start_pivot,
                    end_pivot=segment.end_pivot,
                ),
            }
            nodes_for_bundle: list[RecursiveProofNode] = []
            if graph is not None and request is not None:
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
                        source_kind="complete_history_weekly_batch_candidate_child_graph",
                    )
                    child_nodes.append(
                        RecursiveProofNode.create(
                            node_id=child_wave.wave_id,
                            wave=child_wave,
                            proof_timeframe="weekly",
                            native_window=request.native_window,
                            native_pivot_catalog=request.pivot_catalog,
                            coverage=_coverage_attestation(
                                dataset=weekly,
                                window=request.native_window,
                                start_pivot=child_wave.start_pivot,
                                end_pivot=child_wave.end_pivot,
                            ),
                        )
                    )
                root_kwargs.update(
                    {
                        "child_graph": graph,
                        "child_window": request.native_window,
                        "child_pivot_catalog": request.pivot_catalog,
                        "child_boundary_lineage": request.parent_boundary_lineage,
                        "child_coverage": _coverage_attestation(
                            dataset=weekly,
                            window=request.native_window,
                            start_pivot=segment.start_pivot,
                            end_pivot=segment.end_pivot,
                        ),
                        "child_node_ids": tuple(item.node_id for item in child_nodes),
                    }
                )
                nodes_for_bundle.extend(child_nodes)
            elif segment.wave_id in weekly_coverage_by_role[role]:
                root_kwargs["child_coverage"] = weekly_coverage_by_role[role][segment.wave_id]
            root_node = RecursiveProofNode.create(**root_kwargs)
            nodes_for_bundle.append(root_node)
            segment_bundle = RecursiveProofBundle.create(
                bundle_id=f"complete_history_monthly_segment:{bundle.bundle_id}:{role.value}:{monthly_map.content_hash[:16]}:{segment.wave_id}",
                role=role,
                source_analysis_run_id=analysis_id,
                source_analysis_run_hash=analysis_hash,
                source_degree_resolution_id=resolution_id,
                source_degree_resolution_hash=resolution_hash,
                analysis_cutoff_utc=bundle.shared_cutoff_utc,
                root_node_id=root_node.node_id,
                nodes=tuple(nodes_for_bundle),
                policy=proof_policy,
                shadow_mode=True,
                created_at_utc=timestamp,
            )
            proof = RecursiveMultiTimeframeProofVerifier().verify(segment_bundle, shadow_mode=True)
            return CompleteHistorySegmentProofResult.create(
                role=role,
                monthly_map_content_hash=monthly_map.content_hash,
                segment_id=segment.wave_id,
                weekly_batch_content_hash=(
                    weekly_batches_by_role[role].content_hash
                    if role in weekly_batches_by_role and graph is not None
                    else None
                ),
                proof_result=proof,
            )

        outer_bundles = tuple(make_outer_bundle(role, maps_by_role[role]) for role in roles)
        outer_pair = RecursiveProofBundlePair.create(
            pair_id="",
            primary_bundle=next(item for item in outer_bundles if item.role is RecursiveProofHypothesisRole.PRIMARY),
            alternative_bundle=next(item for item in outer_bundles if item.role is RecursiveProofHypothesisRole.ALTERNATIVE),
            created_at_utc=timestamp,
        )
        proof_result = RecursiveMultiTimeframeProofVerifier().verify_pair(outer_pair, shadow_mode=True)

        segment_proof_results: list[CompleteHistorySegmentProofResult] = []
        for role in roles:
            batch = weekly_batches_by_role.get(role)
            graphs_by_segment = {} if batch is None else {
                graph.parent_candidate_id: graph for graph in batch.graphs
            }
            requests_by_segment = {item.parent_wave.wave_id: item for item in weekly_requests_by_role[role]}
            for segment in maps_by_role[role].completed_segments:
                segment_proof_results.append(
                    make_segment_proof(
                        role,
                        maps_by_role[role],
                        segment,
                        graphs_by_segment.get(segment.wave_id),
                        requests_by_segment.get(segment.wave_id),
                    )
                )

        warnings = [
            "All verification statuses were produced by RecursiveMultiTimeframeProofVerifier, not the candidate provider.",
            "The active outer parent remains candidate-only; completed Monthly map segments are separately prepared for proof.",
            "No SQLite, forecast, resolution, CLI, or active-pipeline mutation occurred.",
            "The versioned active-parent map workflow stopped at the human-approved Monthly or Weekly stage; no Daily or native-4h candidate request was created.",
            "Provider-call accounting: "
            f"attempted={sum(attempted_calls.values())}, accepted={sum(accepted_calls.values())}, "
            f"rejected={sum(rejected_calls.values())}.",
        ]
        if artifact_store is None:
            warnings.append("No local artifact output_directory was supplied; this execution is in-memory only and cannot resume from local artifacts.")
        else:
            warnings.append("Only validated structured Monthly maps and Weekly graph batches were written as content-addressed local artifacts; no hidden reasoning or credentials were persisted.")
        if any(item.proof_result.status is SubdivisionVerificationStatus.NOT_COVERED for item in segment_proof_results):
            warnings.append("At least one completed Monthly segment is not_covered; unavailable lower-timeframe intervals were retained rather than skipped.")
        if approval.max_stage is CompleteHistoryMaximumStage.WEEKLY and any(
            monthly_map.completed_segments and not weekly_requests_by_role[role]
            for role, monthly_map in maps_by_role.items()
        ):
            warnings.append("One or more Monthly maps had completed segments but no fully covered Weekly interval eligible for the batched request.")

        return CompleteHistoryShadowExecution.create(
            execution_id="",
            plan_content_hash=plan.content_hash,
            bundle_content_hash=bundle.content_hash,
            approval_content_hash=approval.content_hash,
            provider=provider_name,
            model=provider_model,
            monthly_maps=tuple(maps_by_role[role] for role in roles),
            segment_proof_results=tuple(segment_proof_results),
            proof_result=proof_result,
            calls_primary=attempted_calls[RecursiveProofHypothesisRole.PRIMARY],
            calls_alternative=attempted_calls[RecursiveProofHypothesisRole.ALTERNATIVE],
            warnings=tuple(warnings),
            created_at_utc=timestamp,
            max_stage=approval.max_stage,
            exact_call_limit=approval.exact_call_limit,
            artifact_output_directory=str(artifact_store.directory) if artifact_store is not None else None,
            created_artifact_content_hashes=tuple(created_artifact_hashes),
            reused_artifact_content_hashes=tuple(reused_artifact_hashes),
            attempted_provider_calls=sum(attempted_calls.values()),
            accepted_provider_calls=sum(accepted_calls.values()),
            rejected_provider_calls=sum(rejected_calls.values()),
        )


__all__ = [
    "CHILD_GRAPH_OUTPUT_SCHEMA",
    "COMPLETE_HISTORY_APPROVAL_SCHEMA_VERSION",
    "COMPLETE_HISTORY_BUNDLE_SCHEMA_VERSION",
    "COMPLETE_HISTORY_DATASET_SCHEMA_VERSION",
    "COMPLETE_HISTORY_EXECUTION_SCHEMA_VERSION",
    "COMPLETE_HISTORY_MONTHLY_MAP_SCHEMA_VERSION",
    "COMPLETE_HISTORY_PLAN_SCHEMA_VERSION",
    "COMPLETE_HISTORY_PROOF_LADDER",
    "COMPLETE_HISTORY_REQUIRED_TIMEFRAMES",
    "COMPLETE_HISTORY_ROOT_CANDIDATE_SCHEMA_VERSION",
    "COMPLETE_HISTORY_STAGE_ARTIFACT_SCHEMA_VERSION",
    "COMPLETE_HISTORY_STAGE_REQUEST_SCHEMA_VERSION",
    "COMPLETE_HISTORY_SHADOW_RUNNER_SCHEMA_VERSION",
    "COMPLETE_HISTORY_SHADOW_POLICY_VERSION",
    "COMPLETE_HISTORY_SEGMENT_PROOF_RESULT_SCHEMA_VERSION",
    "COMPLETE_HISTORY_WEEKLY_BATCH_REQUEST_SCHEMA_VERSION",
    "COMPLETE_HISTORY_WEEKLY_BATCH_SCHEMA_VERSION",
    "BlindCandidateRulesPack",
    "CompleteHistoryBundle",
    "CompleteHistoryCandidateProvider",
    "CompleteHistoryDataset",
    "CompleteHistoryHumanApproval",
    "CompleteHistoryHypothesisPlan",
    "CompleteHistoryLocalArtifactStore",
    "CompleteHistoryMaximumStage",
    "CompleteHistoryMonthlyMap",
    "CompleteHistoryRootCandidate",
    "CompleteHistorySegmentProofResult",
    "CompleteHistoryShadowError",
    "CompleteHistoryShadowExecution",
    "CompleteHistoryShadowPlan",
    "CompleteHistoryShadowPolicy",
    "CompleteHistoryShadowReasonCode",
    "CompleteHistoryShadowRunner",
    "CompleteHistoryShadowStage",
    "CompleteHistoryStageArtifact",
    "CompleteHistoryStageArtifactOutputKind",
    "CompleteHistoryStage1HardRuleCode",
    "CompleteHistoryStage1ScreenResult",
    "CompleteHistoryStagePlan",
    "CompleteHistoryStageRequest",
    "CompleteHistoryWeeklyBatchRequest",
    "CompleteHistoryWeeklyChildGraphBatch",
    "MONTHLY_MAP_OUTPUT_SCHEMA",
    "MONTHLY_ROOT_CANDIDATE_OUTPUT_SCHEMA",
    "WEEKLY_CHILD_GRAPH_BATCH_OUTPUT_SCHEMA",
    "build_complete_history_shadow_plan",
    "blind_candidate_rules_pack_content_hash",
    "complete_history_bundle_content_hash",
    "complete_history_dataset_content_hash",
    "complete_history_human_approval_content_hash",
    "complete_history_monthly_map_content_hash",
    "complete_history_root_candidate_content_hash",
    "complete_history_segment_proof_result_content_hash",
    "complete_history_shadow_execution_content_hash",
    "complete_history_shadow_plan_content_hash",
    "complete_history_shadow_policy",
    "complete_history_shadow_policy_content_hash",
    "complete_history_stage_artifact_content_hash",
    "complete_history_stage_call_limit",
    "complete_history_stage1_screen_content_hash",
    "complete_history_stage_request_content_hash",
    "complete_history_weekly_child_graph_batch_content_hash",
    "complete_history_weekly_batch_request_content_hash",
    "default_blind_candidate_rules_pack",
    "load_complete_history_bundle",
    "screen_complete_history_stage1_child_graph",
    "screen_complete_history_stage1_root",
    "screen_complete_history_monthly_map",
]
