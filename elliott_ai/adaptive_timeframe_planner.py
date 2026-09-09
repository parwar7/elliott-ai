"""Deterministic timeframe planning for adaptive Elliott subdivision questions."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .adaptive_bar_estimation import (
    BarEstimateQuality,
    estimate_native_bars,
    estimate_quality_rank,
)
from .forecast_records import canonical_sha256
from .schema import STANDARD_DEGREES
from .timeframes import (
    ProviderCapabilitySet,
    TimeframeAvailability,
    TimeframeUsePurpose,
    normalize_timeframe_name,
    sort_timeframes,
    timeframe_compatibility_for_purpose,
    timeframe_duration_seconds,
    timeframe_is_finer,
)


ADAPTIVE_TIMEFRAME_PLANNER_SCHEMA_VERSION = "adaptive-timeframe-planner-1.3.0"
ADAPTIVE_TIMEFRAME_PLANNER_POLICY_VERSION = "adaptive-timeframe-planner-policy-1.4.0"
ROOT_TIMEFRAME_PLANNER_POLICY_VERSION = "adaptive-timeframe-planner-policy-1.3.0"
TIMEFRAME_REQUEST_SCHEMA_VERSION = "adaptive-timeframe-request-1.2.0"
TIMEFRAME_DECISION_SCHEMA_VERSION = "adaptive-timeframe-decision-1.3.0"
ROOT_TIMEFRAME_DECISION_SCHEMA_VERSION = "adaptive-timeframe-decision-1.2.0"
PARENT_PROOF_FEASIBILITY_SCHEMA_VERSION = "parent-proof-feasibility-1.0.0"
CHILD_PROOF_RESOLUTION_CHECK_SCHEMA_VERSION = (
    "child-proof-resolution-check-1.0.0"
)

_LEGACY_TIMEFRAME_REQUEST_SCHEMA_VERSIONS = frozenset(
    {
        "adaptive-timeframe-request-1.0.0",
        "adaptive-timeframe-request-1.1.0",
    }
)
_LEGACY_TIMEFRAME_DECISION_SCHEMA_VERSIONS = frozenset(
    {
        "adaptive-timeframe-decision-1.0.0",
        "adaptive-timeframe-decision-1.1.0",
    }
)
_ASSESSMENT_ESTIMATE_PROVENANCE_FIELDS = (
    "estimation_basis",
    "market_calendar_id",
    "session_policy",
    "session_minutes",
    "provider_bar_alignment",
    "estimate_quality",
    "assumptions",
    "structural_interval_start_utc",
    "structural_interval_end_utc",
)


class StructuralQuestion(StrEnum):
    DISCOVER_ROOT_STRUCTURE = "discover_root_structure"
    PROVE_PARENT_SUBDIVISION = "prove_parent_subdivision"
    DISTINGUISH_IMPULSE_FROM_CORRECTION = "distinguish_impulse_from_correction"
    DISTINGUISH_WAVE_3_FROM_WAVE_C = "distinguish_wave_3_from_wave_C"
    DISTINGUISH_FIVE_FROM_THREE = "distinguish_five_from_three"
    RESOLVE_CANDIDATE_PIVOT = "resolve_candidate_pivot"
    INSPECT_ACTIVE_TERMINAL_WAVE = "inspect_active_terminal_wave"
    TEST_DIAGONAL_INTERNALS = "test_diagonal_internals"
    RESOLVE_CONNECTOR_STRUCTURE = "resolve_connector_structure"
    INVESTIGATE_GAP_OR_ORIGIN_CONTROL = "investigate_gap_or_origin_control"


class TimeframePlanningStatus(StrEnum):
    PLANNED = "planned"
    ALREADY_COVERED = "already_covered"
    UNSUPPORTED = "unsupported"
    NOT_COVERED = "not_covered"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONFIGURATION_ERROR = "configuration_error"
    UNRESOLVED = "unresolved"


class TimeframePlanningReason(StrEnum):
    TARGET_BAR_RANGE = "target_bar_range"
    EXPLICIT_SUPPORTED_REQUEST = "explicit_supported_request"
    REQUIRED_NATIVE_RESOLUTION = "required_native_resolution"
    NO_SUPPORTED_LOWER_INTERVAL = "no_supported_lower_interval"
    REDUNDANT_NEARBY_INTERVAL = "redundant_nearby_interval"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"
    BAR_BUDGET_EXCEEDED = "bar_budget_exceeded"
    EXISTING_FULL_COVERAGE = "existing_full_coverage"
    PROVIDER_CAPABILITY_UNAVAILABLE = "provider_capability_unavailable"
    INCOMPATIBLE_WITH_EXPECTED_CHILD_DEGREE = (
        "incompatible_with_expected_child_degree"
    )
    PROOF_POLICY_CONFIGURATION_ERROR = "proof_policy_configuration_error"
    ROOT_TIMEFRAME_REPLAN = "root_timeframe_replan"


class ParentProofFeasibilityStatus(StrEnum):
    FEASIBLE = "feasible"
    NOT_COVERED = "not_covered"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONFIGURATION_ERROR = "configuration_error"


def _timeframe_is_allowed_for_request(
    request: "AdaptiveTimeframeRequest", timeframe: str
) -> bool:
    if request.structural_question is StructuralQuestion.DISCOVER_ROOT_STRUCTURE:
        return True
    if request.expected_child_degree is None:
        return False
    return timeframe_compatibility_for_purpose(
        request.expected_child_degree,
        timeframe,
        purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
        parent_degree=request.parent_degree,
        parent_timeframe=request.parent_timeframe,
    ).compatible


def _utc_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an offset.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


class _Contract:
    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, StrEnum):
                return value.value
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, Mapping):
                return {str(key): convert(item) for key, item in sorted(value.items())}
            method = getattr(value, "to_dict", None)
            if callable(method):
                return convert(method())
            return value

        return {item.name: convert(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class TimeframeEvidenceCoverage(_Contract):
    timeframe: str
    first_timestamp_utc: str | None
    last_completed_timestamp_utc: str | None
    native_bar_count: int
    full_parent_coverage: bool
    native: bool
    source_reference: str
    provider: str | None = None
    feed_identity: str | None = None
    session_policy: str | None = None
    adjustment_policy: str | None = None
    source_hash: str | None = None
    source_interface: str | None = None
    provider_symbol: str | None = None
    exchange: str | None = None
    mic: str | None = None
    timezone: str | None = None
    dividend_adjustment: str | None = None
    volume_adjustment: str | None = None
    price_basis: str | None = None
    provider_interval_alias: str | None = None
    timestamp_semantics: str | None = None
    provider_bar_alignment: str | None = None
    feed_family_hash: str | None = None
    stream_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", normalize_timeframe_name(self.timeframe))
        if self.first_timestamp_utc is not None:
            object.__setattr__(self, "first_timestamp_utc", _utc_text(self.first_timestamp_utc, field_name="first_timestamp_utc"))
        if self.last_completed_timestamp_utc is not None:
            object.__setattr__(self, "last_completed_timestamp_utc", _utc_text(self.last_completed_timestamp_utc, field_name="last_completed_timestamp_utc"))
        if not isinstance(self.native_bar_count, int) or self.native_bar_count < 0:
            raise ValueError("native_bar_count must be a non-negative integer.")
        if not isinstance(self.full_parent_coverage, bool) or not isinstance(self.native, bool):
            raise TypeError("full_parent_coverage and native must be boolean.")
        object.__setattr__(self, "source_reference", _text(self.source_reference, field_name="source_reference"))
        for name in (
            "provider",
            "feed_identity",
            "session_policy",
            "adjustment_policy",
            "source_interface",
            "provider_symbol",
            "exchange",
            "mic",
            "timezone",
            "dividend_adjustment",
            "volume_adjustment",
            "price_basis",
            "provider_interval_alias",
            "timestamp_semantics",
            "provider_bar_alignment",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, field_name=name))
        for name in ("source_hash", "feed_family_hash", "stream_hash"):
            value = getattr(self, name)
            if value is not None:
                text = _text(value, field_name=name).lower()
                if len(text) != 64 or any(
                    character not in "0123456789abcdef" for character in text
                ):
                    raise ValueError(f"{name} must be a lowercase SHA-256 hash.")
                object.__setattr__(self, name, text)

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        for name in (
            "provider",
            "feed_identity",
            "session_policy",
            "adjustment_policy",
            "source_hash",
            "source_interface",
            "provider_symbol",
            "exchange",
            "mic",
            "timezone",
            "dividend_adjustment",
            "volume_adjustment",
            "price_basis",
            "provider_interval_alias",
            "timestamp_semantics",
            "provider_bar_alignment",
            "feed_family_hash",
            "stream_hash",
        ):
            if getattr(self, name) is None:
                payload.pop(name, None)
        return payload


@dataclass(frozen=True, slots=True)
class AdaptiveTimeframeRequest(_Contract):
    request_id: str
    symbol: str
    parent_candidate_id: str
    parent_degree: str | None
    parent_timeframe: str | None
    parent_start_utc: str
    parent_end_utc: str
    structural_question: StructuralQuestion
    expected_child_degree: str | None
    current_evidence_coverage: tuple[TimeframeEvidenceCoverage, ...]
    desired_native_bar_minimum: int
    desired_native_bar_maximum: int
    provider_capability_set_hash: str
    provider_supported_intervals: tuple[str, ...]
    requested_timeframe: str | None
    reason_code: str
    cutoff_utc: str
    maximum_request_budget: int
    requests_already_used: int
    maximum_bar_budget: int
    created_at_utc: str
    schema_version: str = TIMEFRAME_REQUEST_SCHEMA_VERSION
    policy_version: str = ADAPTIVE_TIMEFRAME_PLANNER_POLICY_VERSION
    content_hash: str = ""
    requested_data_start_utc: str | None = None
    requested_data_end_utc: str | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "symbol", "parent_candidate_id", "reason_code", "schema_version", "policy_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(self, "structural_question", StructuralQuestion(self.structural_question))
        root_discovery = (
            self.structural_question is StructuralQuestion.DISCOVER_ROOT_STRUCTURE
        )
        if root_discovery:
            if self.parent_degree is not None or self.expected_child_degree is not None:
                raise ValueError(
                    "Root discovery cannot assign a synthetic parent or expected child degree."
                )
            if self.parent_timeframe is not None:
                raise ValueError(
                    "Root discovery cannot assign a synthetic Elliott parent timeframe."
                )
        else:
            if (
                self.parent_degree not in STANDARD_DEGREES
                or self.parent_degree == "Unassigned"
            ):
                raise ValueError("parent_degree must be an assigned standard degree.")
            if (
                self.expected_child_degree not in STANDARD_DEGREES
                or self.expected_child_degree == "Unassigned"
            ):
                raise ValueError(
                    "expected_child_degree must be an assigned standard degree."
                )
            if self.parent_timeframe is None:
                raise ValueError("parent_timeframe is required after root selection.")
            object.__setattr__(
                self,
                "parent_timeframe",
                normalize_timeframe_name(self.parent_timeframe),
            )
        for name in ("parent_start_utc", "parent_end_utc", "cutoff_utc", "created_at_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        if _utc(self.parent_end_utc) <= _utc(self.parent_start_utc):
            raise ValueError("parent_end_utc must be after parent_start_utc.")
        if _utc(self.parent_end_utc) > _utc(self.cutoff_utc):
            raise ValueError("Parent interval cannot end after the analysis cutoff.")
        requested_data_start = self.requested_data_start_utc or self.parent_start_utc
        requested_data_end = self.requested_data_end_utc or self.parent_end_utc
        object.__setattr__(
            self,
            "requested_data_start_utc",
            _utc_text(requested_data_start, field_name="requested_data_start_utc"),
        )
        object.__setattr__(
            self,
            "requested_data_end_utc",
            _utc_text(requested_data_end, field_name="requested_data_end_utc"),
        )
        if _utc(self.requested_data_end_utc) <= _utc(self.requested_data_start_utc):
            raise ValueError(
                "requested_data_end_utc must be after requested_data_start_utc."
            )
        if _utc(self.requested_data_start_utc) > _utc(self.parent_start_utc):
            raise ValueError(
                "Requested data must begin no later than the structural interval."
            )
        if _utc(self.requested_data_end_utc) < _utc(self.parent_end_utc):
            raise ValueError(
                "Requested data must cover the complete structural interval."
            )
        if _utc(self.requested_data_end_utc) > _utc(self.cutoff_utc):
            raise ValueError("Requested data cannot extend past the analysis cutoff.")
        coverage = tuple(
            item if isinstance(item, TimeframeEvidenceCoverage) else TimeframeEvidenceCoverage(**dict(item))
            for item in self.current_evidence_coverage
        )
        if len({item.timeframe for item in coverage}) != len(coverage):
            raise ValueError("current_evidence_coverage cannot repeat a timeframe.")
        object.__setattr__(self, "current_evidence_coverage", coverage)
        if not 1 <= self.desired_native_bar_minimum <= self.desired_native_bar_maximum:
            raise ValueError("Desired native bar range is invalid.")
        if len(self.provider_capability_set_hash) != 64:
            raise ValueError("provider_capability_set_hash must be SHA-256.")
        supported = sort_timeframes(self.provider_supported_intervals)
        object.__setattr__(self, "provider_supported_intervals", supported)
        if self.requested_timeframe is not None:
            object.__setattr__(self, "requested_timeframe", normalize_timeframe_name(self.requested_timeframe))
        for name in ("maximum_request_budget", "maximum_bar_budget"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if not isinstance(self.requests_already_used, int) or not 0 <= self.requests_already_used <= self.maximum_request_budget:
            raise ValueError("requests_already_used is outside the request budget.")
        if self.content_hash and self.content_hash != adaptive_timeframe_request_content_hash(self):
            raise ValueError("AdaptiveTimeframeRequest content_hash does not match its payload.")

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.schema_version in _LEGACY_TIMEFRAME_REQUEST_SCHEMA_VERSIONS:
            payload.pop("requested_data_start_utc", None)
            payload.pop("requested_data_end_utc", None)
        return payload

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveTimeframeRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("request_id"):
            values["request_id"] = "tf_request_" + canonical_sha256(
                {
                    "symbol": values.get("symbol"),
                    "parent": values.get("parent_candidate_id"),
                    "question": str(values.get("structural_question")),
                    "created_at": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=adaptive_timeframe_request_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "AdaptiveTimeframeRequest":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != adaptive_timeframe_request_content_hash(result):
            raise ValueError("AdaptiveTimeframeRequest content_hash does not match its payload.")
        return result


def adaptive_timeframe_request_content_hash(
    value: AdaptiveTimeframeRequest | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveTimeframeRequest) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") in _LEGACY_TIMEFRAME_REQUEST_SCHEMA_VERSIONS:
        payload.pop("requested_data_start_utc", None)
        payload.pop("requested_data_end_utc", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class TimeframeCandidateAssessment(_Contract):
    timeframe: str
    estimated_native_bars: int
    native_available: bool
    useful: bool
    redundant: bool
    score: tuple[float, ...]
    reason: str
    estimation_basis: str = "wall_clock_fallback"
    market_calendar_id: str | None = None
    session_policy: str = "unknown"
    session_minutes: int | None = None
    provider_bar_alignment: str = "unknown"
    estimate_quality: BarEstimateQuality = BarEstimateQuality.COARSE_FALLBACK
    assumptions: tuple[str, ...] = ()
    structural_interval_start_utc: str | None = None
    structural_interval_end_utc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", normalize_timeframe_name(self.timeframe))
        if not isinstance(self.estimated_native_bars, int) or self.estimated_native_bars < 0:
            raise ValueError("estimated_native_bars must be non-negative.")
        if not all(isinstance(item, bool) for item in (self.native_available, self.useful, self.redundant)):
            raise TypeError("Assessment flags must be boolean.")
        object.__setattr__(self, "score", tuple(float(item) for item in self.score))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason"))
        object.__setattr__(
            self,
            "estimation_basis",
            _text(self.estimation_basis, field_name="estimation_basis"),
        )
        if self.market_calendar_id is not None:
            object.__setattr__(
                self,
                "market_calendar_id",
                _text(self.market_calendar_id, field_name="market_calendar_id"),
            )
        object.__setattr__(
            self,
            "session_policy",
            _text(self.session_policy, field_name="session_policy"),
        )
        if self.session_minutes is not None and (
            not isinstance(self.session_minutes, int)
            or isinstance(self.session_minutes, bool)
            or self.session_minutes < 1
        ):
            raise ValueError("session_minutes must be a positive integer or null.")
        object.__setattr__(
            self,
            "provider_bar_alignment",
            _text(
                self.provider_bar_alignment,
                field_name="provider_bar_alignment",
            ),
        )
        object.__setattr__(
            self,
            "estimate_quality",
            BarEstimateQuality(self.estimate_quality),
        )
        object.__setattr__(
            self,
            "assumptions",
            tuple(str(item) for item in self.assumptions),
        )
        for name in (
            "structural_interval_start_utc",
            "structural_interval_end_utc",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _utc_text(value, field_name=name),
                )


@dataclass(frozen=True, slots=True)
class ChildProofResolutionCheck(_Contract):
    timeframe: str
    provider_alias: str | None
    estimated_native_bars: int
    provider_native: bool
    finer_than_parent: bool
    coverage_requestable: bool
    within_bar_budget: bool
    provider_metadata_compatible: bool
    viable: bool
    reason_codes: tuple[str, ...]
    schema_version: str = CHILD_PROOF_RESOLUTION_CHECK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", normalize_timeframe_name(self.timeframe))
        if self.provider_alias is not None:
            object.__setattr__(
                self,
                "provider_alias",
                _text(self.provider_alias, field_name="provider_alias"),
            )
        if (
            not isinstance(self.estimated_native_bars, int)
            or isinstance(self.estimated_native_bars, bool)
            or self.estimated_native_bars < 0
        ):
            raise ValueError("estimated_native_bars must be a nonnegative integer.")
        for name in (
            "provider_native",
            "finer_than_parent",
            "coverage_requestable",
            "within_bar_budget",
            "provider_metadata_compatible",
            "viable",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        reasons = tuple(
            _text(item, field_name="reason_code") for item in self.reason_codes
        )
        if len(reasons) != len(set(reasons)):
            raise ValueError("reason_codes cannot repeat.")
        if self.viable != (not reasons):
            raise ValueError("viable must be true exactly when reason_codes is empty.")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self,
            "schema_version",
            _text(self.schema_version, field_name="schema_version"),
        )


@dataclass(frozen=True, slots=True)
class ParentProofFeasibility(_Contract):
    feasibility_id: str
    parent_candidate_id: str
    parent_degree: str
    expected_child_degree: str
    parent_evidence_timeframe: str
    parent_start_utc: str
    parent_end_utc: str
    feasible: bool
    status: ParentProofFeasibilityStatus
    viable_timeframes: tuple[str, ...]
    selected_timeframe: str | None
    rejected_timeframes: Mapping[str, tuple[str, ...]]
    estimated_native_bars: Mapping[str, int]
    resolution_checks: tuple[ChildProofResolutionCheck, ...]
    blockers: tuple[str, ...]
    provider_capability_set_hash: str
    created_at_utc: str
    purpose: TimeframeUsePurpose = TimeframeUsePurpose.SUBDIVISION_PROOF
    schema_version: str = PARENT_PROOF_FEASIBILITY_SCHEMA_VERSION
    policy_version: str = ADAPTIVE_TIMEFRAME_PLANNER_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "feasibility_id",
            "parent_candidate_id",
            "parent_degree",
            "expected_child_degree",
            "schema_version",
            "policy_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(
            self,
            "parent_evidence_timeframe",
            normalize_timeframe_name(self.parent_evidence_timeframe),
        )
        for name in ("parent_start_utc", "parent_end_utc", "created_at_utc"):
            object.__setattr__(
                self,
                name,
                _utc_text(getattr(self, name), field_name=name),
            )
        if _utc(self.parent_end_utc) <= _utc(self.parent_start_utc):
            raise ValueError("parent_end_utc must follow parent_start_utc.")
        if not isinstance(self.feasible, bool):
            raise TypeError("feasible must be boolean.")
        object.__setattr__(self, "status", ParentProofFeasibilityStatus(self.status))
        object.__setattr__(self, "purpose", TimeframeUsePurpose(self.purpose))
        if self.purpose is not TimeframeUsePurpose.SUBDIVISION_PROOF:
            raise ValueError("ParentProofFeasibility is restricted to subdivision proof.")
        viable = tuple(normalize_timeframe_name(item) for item in self.viable_timeframes)
        if len(viable) != len(set(viable)):
            raise ValueError("viable_timeframes cannot repeat.")
        object.__setattr__(self, "viable_timeframes", viable)
        if self.selected_timeframe is not None:
            selected = normalize_timeframe_name(self.selected_timeframe)
            if selected not in viable:
                raise ValueError("selected_timeframe must be one of viable_timeframes.")
            object.__setattr__(self, "selected_timeframe", selected)
        rejected: dict[str, tuple[str, ...]] = {}
        for timeframe, reasons in self.rejected_timeframes.items():
            canonical = normalize_timeframe_name(timeframe)
            reason_values = tuple(_text(item, field_name="rejection_reason") for item in reasons)
            if not reason_values:
                raise ValueError("A rejected timeframe requires at least one reason.")
            rejected[canonical] = reason_values
        if set(rejected) & set(viable):
            raise ValueError("A timeframe cannot be both viable and rejected.")
        object.__setattr__(
            self,
            "rejected_timeframes",
            MappingProxyType(dict(sorted(rejected.items()))),
        )
        estimates: dict[str, int] = {}
        for timeframe, bars in self.estimated_native_bars.items():
            canonical = normalize_timeframe_name(timeframe)
            if not isinstance(bars, int) or isinstance(bars, bool) or bars < 0:
                raise ValueError("Estimated bars must be nonnegative integers.")
            estimates[canonical] = bars
        object.__setattr__(
            self,
            "estimated_native_bars",
            MappingProxyType(dict(sorted(estimates.items()))),
        )
        checks = tuple(
            item
            if isinstance(item, ChildProofResolutionCheck)
            else ChildProofResolutionCheck(**dict(item))
            for item in self.resolution_checks
        )
        if len({item.timeframe for item in checks}) != len(checks):
            raise ValueError("resolution_checks cannot repeat a timeframe.")
        if set(self.estimated_native_bars) != {item.timeframe for item in checks}:
            raise ValueError("estimated_native_bars must cover every resolution check.")
        object.__setattr__(self, "resolution_checks", checks)
        blockers = tuple(_text(item, field_name="blocker") for item in self.blockers)
        object.__setattr__(self, "blockers", blockers)
        if len(self.provider_capability_set_hash) != 64:
            raise ValueError("provider_capability_set_hash must be SHA-256.")
        if self.feasible != (
            self.status is ParentProofFeasibilityStatus.FEASIBLE
            and self.selected_timeframe is not None
        ):
            raise ValueError("feasible is inconsistent with status/selection.")
        if self.content_hash and self.content_hash != parent_proof_feasibility_content_hash(self):
            raise ValueError("ParentProofFeasibility content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ParentProofFeasibility":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("feasibility_id"):
            values["feasibility_id"] = "proof_feasibility_" + canonical_sha256(
                {
                    "parent": values.get("parent_candidate_id"),
                    "capabilities": values.get("provider_capability_set_hash"),
                    "created": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(
            result,
            content_hash=parent_proof_feasibility_content_hash(result),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ParentProofFeasibility":
        raw = dict(value)
        raw["resolution_checks"] = tuple(
            ChildProofResolutionCheck(**dict(item))
            for item in raw.get("resolution_checks", ())
        )
        result = cls(**raw)
        if verify_hash and result.content_hash != parent_proof_feasibility_content_hash(result):
            raise ValueError("ParentProofFeasibility content_hash does not match its payload.")
        return result


def parent_proof_feasibility_content_hash(
    value: ParentProofFeasibility | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ParentProofFeasibility) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def timeframe_can_resolve_child_structure(
    *,
    parent_degree: str,
    parent_evidence_timeframe: str,
    expected_child_degree: str,
    proposed_proof_timeframe: str,
    parent_start_utc: str,
    parent_end_utc: str,
    provider_capabilities: ProviderCapabilitySet,
    estimated_native_bars: int,
    maximum_bar_budget: int,
) -> ChildProofResolutionCheck:
    """Return one deterministic, purpose-aware proof-resolution check."""

    start = _utc(_utc_text(parent_start_utc, field_name="parent_start_utc"))
    end = _utc(_utc_text(parent_end_utc, field_name="parent_end_utc"))
    if end <= start:
        raise ValueError("parent_end_utc must follow parent_start_utc.")
    if (
        not isinstance(maximum_bar_budget, int)
        or isinstance(maximum_bar_budget, bool)
        or maximum_bar_budget < 1
    ):
        raise ValueError("maximum_bar_budget must be a positive integer.")
    if (
        not isinstance(estimated_native_bars, int)
        or isinstance(estimated_native_bars, bool)
        or estimated_native_bars < 0
    ):
        raise ValueError("estimated_native_bars must be a nonnegative integer.")

    timeframe = normalize_timeframe_name(proposed_proof_timeframe)
    relation = timeframe_compatibility_for_purpose(
        expected_child_degree,
        timeframe,
        purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
        parent_degree=parent_degree,
        parent_timeframe=parent_evidence_timeframe,
    )
    capability = next(
        (
            item
            for item in provider_capabilities.capabilities
            if item.canonical_name == timeframe
        ),
        None,
    )
    parent_capability = next(
        (
            item
            for item in provider_capabilities.capabilities
            if item.canonical_name
            == normalize_timeframe_name(parent_evidence_timeframe)
        ),
        None,
    )
    provider_native = bool(
        capability is not None
        and capability.availability is TimeframeAvailability.AVAILABLE
        and capability.native_available
        and not capability.derived
    )
    coverage_requestable = bool(provider_native and estimated_native_bars > 0)
    within_bar_budget = bool(
        estimated_native_bars > 0
        and estimated_native_bars <= maximum_bar_budget
    )
    provider_metadata_compatible = bool(
        capability is not None
        and capability.provider == provider_capabilities.provider
        and (
            parent_capability is None
            or (
                parent_capability.provider == capability.provider
                and parent_capability.session_policy == capability.session_policy
                and parent_capability.adjustment_policy
                == capability.adjustment_policy
            )
        )
    )
    reasons = list(relation.reason_codes)
    if capability is None or capability.availability is not TimeframeAvailability.AVAILABLE:
        reasons.append("provider_capability_unavailable")
    elif not capability.native_available or capability.derived:
        reasons.append("proof_timeframe_not_provider_native")
    if estimated_native_bars <= 0:
        reasons.append("native_bar_estimate_unavailable")
    if estimated_native_bars > maximum_bar_budget:
        reasons.append("bar_budget_exceeded")
    if capability is not None and not provider_metadata_compatible:
        reasons.append("provider_session_adjustment_incompatible")
    reasons = list(dict.fromkeys(reasons))
    return ChildProofResolutionCheck(
        timeframe=timeframe,
        provider_alias=(capability.provider_alias if capability is not None else None),
        estimated_native_bars=estimated_native_bars,
        provider_native=provider_native,
        finer_than_parent=timeframe_is_finer(timeframe, parent_evidence_timeframe),
        coverage_requestable=coverage_requestable,
        within_bar_budget=within_bar_budget,
        provider_metadata_compatible=provider_metadata_compatible,
        viable=not reasons,
        reason_codes=tuple(reasons),
    )


def assess_parent_proof_feasibility(
    request: AdaptiveTimeframeRequest,
    capabilities: ProviderCapabilitySet,
) -> ParentProofFeasibility:
    """Preflight all native finer resolutions before a proof request."""

    if request.structural_question is StructuralQuestion.DISCOVER_ROOT_STRUCTURE:
        raise ValueError("Root discovery has no parent subdivision feasibility.")
    if request.provider_capability_set_hash != capabilities.content_hash:
        raise ValueError("Proof feasibility is not bound to this capability set.")
    if request.parent_degree is None or request.parent_timeframe is None:
        raise ValueError("Proof feasibility requires parent degree and timeframe.")
    if request.expected_child_degree is None:
        raise ValueError("Proof feasibility requires the expected child degree.")

    proposed_timeframes = [item.canonical_name for item in capabilities.capabilities]
    if (
        request.requested_timeframe is not None
        and request.requested_timeframe not in proposed_timeframes
    ):
        proposed_timeframes.append(request.requested_timeframe)
    checks: list[ChildProofResolutionCheck] = []
    scores: dict[str, tuple[float, ...]] = {}
    for timeframe in sort_timeframes(proposed_timeframes):
        capability = next(
            (
                item
                for item in capabilities.capabilities
                if item.canonical_name == timeframe
            ),
            None,
        )
        if capability is None:
            bars = 0
            quality_rank = float(estimate_quality_rank(BarEstimateQuality.UNAVAILABLE))
        else:
            estimate = estimate_native_bars(
                structural_start_utc=request.parent_start_utc,
                structural_end_utc=request.parent_end_utc,
                capability=capability,
            )
            bars = estimate.estimated_native_bars
            quality_rank = float(estimate_quality_rank(estimate.estimate_quality))
        check = timeframe_can_resolve_child_structure(
            parent_degree=request.parent_degree,
            parent_evidence_timeframe=request.parent_timeframe,
            expected_child_degree=request.expected_child_degree,
            proposed_proof_timeframe=timeframe,
            parent_start_utc=request.parent_start_utc,
            parent_end_utc=request.parent_end_utc,
            provider_capabilities=capabilities,
            estimated_native_bars=bars,
            maximum_bar_budget=request.maximum_bar_budget,
        )
        checks.append(check)
        distance = (
            0
            if request.desired_native_bar_minimum
            <= bars
            <= request.desired_native_bar_maximum
            else request.desired_native_bar_minimum - bars
            if bars < request.desired_native_bar_minimum
            else bars - request.desired_native_bar_maximum
        )
        in_target = (
            request.desired_native_bar_minimum
            <= bars
            <= request.desired_native_bar_maximum
        )
        scores[timeframe] = (
            quality_rank,
            0.0 if in_target else 1.0,
            float(distance),
            float(bars),
            float(timeframe_duration_seconds(timeframe)),
        )

    viable_checks = sorted(
        (item for item in checks if item.viable),
        key=lambda item: (scores[item.timeframe], item.timeframe),
    )
    viable_timeframes = tuple(item.timeframe for item in viable_checks)
    if request.requested_timeframe is not None:
        explicit = normalize_timeframe_name(request.requested_timeframe)
        selected = explicit if explicit in viable_timeframes else None
    else:
        midpoint = (
            request.desired_native_bar_minimum
            + request.desired_native_bar_maximum
        ) / 2.0
        covered = [
            item
            for item in request.current_evidence_coverage
            if item.full_parent_coverage
            and item.native
            and item.timeframe in viable_timeframes
            and item.native_bar_count <= request.maximum_bar_budget
        ]
        selected = (
            min(
                covered,
                key=lambda item: (
                    abs(item.native_bar_count - midpoint),
                    timeframe_duration_seconds(item.timeframe),
                    item.timeframe,
                ),
            ).timeframe
            if covered
            else viable_timeframes[0]
            if viable_timeframes
            else None
        )

    relation = timeframe_compatibility_for_purpose(
        request.expected_child_degree,
        (
            request.requested_timeframe
            or next(iter(capabilities.supported_timeframes), request.parent_timeframe)
        ),
        purpose=TimeframeUsePurpose.SUBDIVISION_PROOF,
        parent_degree=request.parent_degree,
        parent_timeframe=request.parent_timeframe,
    )
    if selected is not None:
        status = ParentProofFeasibilityStatus.FEASIBLE
        blockers: tuple[str, ...] = ()
    elif any(
        code in {
            "child_degree_invalid",
            "parent_degree_invalid",
            "direct_child_degree_descent_invalid",
            "parent_evidence_timeframe_missing",
            "parent_evidence_timeframe_invalid",
        }
        for code in relation.reason_codes
    ):
        status = ParentProofFeasibilityStatus.CONFIGURATION_ERROR
        blockers = relation.reason_codes
    elif any(
        item.finer_than_parent
        and item.provider_native
        and item.coverage_requestable
        and item.provider_metadata_compatible
        and not item.within_bar_budget
        for item in checks
    ):
        status = ParentProofFeasibilityStatus.BUDGET_EXHAUSTED
        blockers = ("bar_budget_exceeded",)
    elif any(
        item.finer_than_parent
        and item.provider_native
        and not item.provider_metadata_compatible
        for item in checks
    ):
        status = ParentProofFeasibilityStatus.CONFIGURATION_ERROR
        blockers = ("provider_session_adjustment_incompatible",)
    else:
        status = ParentProofFeasibilityStatus.NOT_COVERED
        blockers = tuple(
            sorted(
                {
                    code
                    for item in checks
                    for code in item.reason_codes
                    if code
                    in {
                        "provider_capability_unavailable",
                        "proof_timeframe_not_provider_native",
                        "proof_timeframe_not_finer",
                        "native_bar_estimate_unavailable",
                    }
                }
            )
        ) or ("no_supported_lower_interval",)

    rejected = {
        item.timeframe: item.reason_codes for item in checks if not item.viable
    }
    estimates = {item.timeframe: item.estimated_native_bars for item in checks}
    return ParentProofFeasibility.create(
        feasibility_id="",
        parent_candidate_id=request.parent_candidate_id,
        parent_degree=request.parent_degree,
        expected_child_degree=request.expected_child_degree,
        parent_evidence_timeframe=request.parent_timeframe,
        parent_start_utc=request.parent_start_utc,
        parent_end_utc=request.parent_end_utc,
        feasible=status is ParentProofFeasibilityStatus.FEASIBLE,
        status=status,
        viable_timeframes=viable_timeframes,
        selected_timeframe=selected,
        rejected_timeframes=rejected,
        estimated_native_bars=estimates,
        resolution_checks=tuple(checks),
        blockers=blockers,
        provider_capability_set_hash=capabilities.content_hash,
        created_at_utc=request.created_at_utc,
    )


@dataclass(frozen=True, slots=True)
class AdaptiveTimeframeDecision(_Contract):
    decision_id: str
    request_id: str
    request_content_hash: str
    status: TimeframePlanningStatus
    selected_timeframe: str | None
    provider_alias: str | None
    estimated_native_bars: int | None
    reason_code: TimeframePlanningReason
    stop_reason: str
    assessments: tuple[TimeframeCandidateAssessment, ...]
    requests_after_decision: int
    created_at_utc: str
    proof_feasibility: ParentProofFeasibility | None = None
    schema_version: str = TIMEFRAME_DECISION_SCHEMA_VERSION
    policy_version: str = ADAPTIVE_TIMEFRAME_PLANNER_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("decision_id", "request_id", "request_content_hash", "stop_reason", "created_at_utc", "schema_version", "policy_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(self, "status", TimeframePlanningStatus(self.status))
        object.__setattr__(self, "reason_code", TimeframePlanningReason(self.reason_code))
        if self.selected_timeframe is not None:
            object.__setattr__(self, "selected_timeframe", normalize_timeframe_name(self.selected_timeframe))
        if self.estimated_native_bars is not None and self.estimated_native_bars < 0:
            raise ValueError("estimated_native_bars must be non-negative.")
        assessments = tuple(
            item if isinstance(item, TimeframeCandidateAssessment) else TimeframeCandidateAssessment(**dict(item))
            for item in self.assessments
        )
        object.__setattr__(self, "assessments", assessments)
        if self.proof_feasibility is not None:
            feasibility = (
                self.proof_feasibility
                if isinstance(self.proof_feasibility, ParentProofFeasibility)
                else ParentProofFeasibility.from_dict(self.proof_feasibility)
            )
            if feasibility.parent_candidate_id == "analysis_scope":
                raise ValueError("Root decisions cannot carry parent proof feasibility.")
            if self.selected_timeframe is not None and (
                feasibility.selected_timeframe != self.selected_timeframe
            ):
                raise ValueError(
                    "Planner selection must match the proof-feasibility selection."
                )
            object.__setattr__(self, "proof_feasibility", feasibility)
        if not isinstance(self.requests_after_decision, int) or self.requests_after_decision < 0:
            raise ValueError("requests_after_decision must be non-negative.")
        if self.content_hash and self.content_hash != adaptive_timeframe_decision_content_hash(self):
            raise ValueError("AdaptiveTimeframeDecision content_hash does not match its payload.")

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.schema_version in _LEGACY_TIMEFRAME_DECISION_SCHEMA_VERSIONS:
            for assessment in payload.get("assessments", ()):
                for name in _ASSESSMENT_ESTIMATE_PROVENANCE_FIELDS:
                    assessment.pop(name, None)
        if self.proof_feasibility is None:
            payload.pop("proof_feasibility", None)
        return payload

    @classmethod
    def create(cls, **values: Any) -> "AdaptiveTimeframeDecision":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("decision_id"):
            values["decision_id"] = "tf_decision_" + canonical_sha256(
                {"request": values.get("request_content_hash"), "status": str(values.get("status"))}
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=adaptive_timeframe_decision_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "AdaptiveTimeframeDecision":
        raw = dict(value)
        if raw.get("proof_feasibility") is not None:
            raw["proof_feasibility"] = ParentProofFeasibility.from_dict(
                raw["proof_feasibility"]
            )
        result = cls(**raw)
        if verify_hash and result.content_hash != adaptive_timeframe_decision_content_hash(result):
            raise ValueError("AdaptiveTimeframeDecision content_hash does not match its payload.")
        return result


def adaptive_timeframe_decision_content_hash(
    value: AdaptiveTimeframeDecision | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, AdaptiveTimeframeDecision) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


class AdaptiveTimeframePlanner:
    """Select one bounded, provider-advertised resolution for one question."""

    def plan(
        self,
        request: AdaptiveTimeframeRequest,
        capabilities: ProviderCapabilitySet,
    ) -> AdaptiveTimeframeDecision:
        if request.provider_capability_set_hash != capabilities.content_hash:
            raise ValueError("Timeframe request is not bound to this provider capability set.")
        if tuple(request.provider_supported_intervals) != sort_timeframes(capabilities.supported_timeframes):
            raise ValueError("Timeframe request provider intervals do not match capabilities.")
        timestamp = request.created_at_utc
        root_discovery = (
            request.structural_question is StructuralQuestion.DISCOVER_ROOT_STRUCTURE
        )
        proof_feasibility = (
            None
            if root_discovery
            else assess_parent_proof_feasibility(request, capabilities)
        )
        if request.requests_already_used >= request.maximum_request_budget:
            return self._decision(
                request,
                status=TimeframePlanningStatus.BUDGET_EXHAUSTED,
                reason=TimeframePlanningReason.REQUEST_BUDGET_EXHAUSTED,
                stop_reason="Maximum timeframe-request budget is exhausted.",
                assessments=(),
                selected=None,
                provider_alias=None,
                bars=None,
                timestamp=timestamp,
                proof_feasibility=proof_feasibility,
            )
        existing_full = {
            item.timeframe
            for item in request.current_evidence_coverage
            if item.full_parent_coverage and item.native
        }
        if (
            request.requested_timeframe in existing_full
            and _timeframe_is_allowed_for_request(
                request,
                request.requested_timeframe,
            )
        ):
            return self._decision(
                request,
                status=TimeframePlanningStatus.ALREADY_COVERED,
                reason=TimeframePlanningReason.EXISTING_FULL_COVERAGE,
                stop_reason="The explicitly requested native timeframe already fully covers the parent.",
                assessments=(),
                selected=request.requested_timeframe,
                provider_alias=capabilities.available(request.requested_timeframe).provider_alias if capabilities.available(request.requested_timeframe) else None,
                bars=next(item.native_bar_count for item in request.current_evidence_coverage if item.timeframe == request.requested_timeframe),
                timestamp=timestamp,
                increment=False,
                proof_feasibility=proof_feasibility,
            )

        # A provider call is unnecessary when a native dataset already gives
        # the requested focused bar view.  Prefer the closest in-range view
        # deterministically; finer data may still be requested later for a
        # different structural question.
        if request.requested_timeframe is None:
            covered_views = [
                item
                for item in request.current_evidence_coverage
                if item.timeframe in existing_full
                and (
                    root_discovery
                    or timeframe_is_finer(item.timeframe, request.parent_timeframe)
                )
                and _timeframe_is_allowed_for_request(request, item.timeframe)
                and (
                    not root_discovery
                    or request.desired_native_bar_minimum
                    <= item.native_bar_count
                    <= request.desired_native_bar_maximum
                )
                and item.native_bar_count <= request.maximum_bar_budget
            ]
            if covered_views:
                midpoint = (
                    request.desired_native_bar_minimum
                    + request.desired_native_bar_maximum
                ) / 2.0
                covered = min(
                    covered_views,
                    key=lambda item: (
                        abs(item.native_bar_count - midpoint),
                        timeframe_duration_seconds(item.timeframe),
                        item.timeframe,
                    ),
                )
                capability = capabilities.available(covered.timeframe)
                return self._decision(
                    request,
                    status=TimeframePlanningStatus.ALREADY_COVERED,
                    reason=TimeframePlanningReason.EXISTING_FULL_COVERAGE,
                    stop_reason=(
                        "Existing native evidence already fully covers the parent "
                        "within the requested focused bar range."
                    ),
                    assessments=(),
                    selected=covered.timeframe,
                    provider_alias=(capability.provider_alias if capability else None),
                    bars=covered.native_bar_count,
                    timestamp=timestamp,
                    increment=False,
                    proof_feasibility=proof_feasibility,
                )

        assessments: list[TimeframeCandidateAssessment] = []
        selected_capability = None
        selected_bars = None
        explicit = request.requested_timeframe
        for capability in capabilities.capabilities:
            timeframe = capability.canonical_name
            estimate = estimate_native_bars(
                structural_start_utc=request.parent_start_utc,
                structural_end_utc=request.parent_end_utc,
                capability=capability,
            )
            bars = estimate.estimated_native_bars
            lower = root_discovery or timeframe_is_finer(
                timeframe, request.parent_timeframe
            )
            proof_check = (
                next(
                    item
                    for item in proof_feasibility.resolution_checks
                    if item.timeframe == timeframe
                )
                if proof_feasibility is not None
                else None
            )
            native = (
                proof_check.provider_native
                if proof_check is not None
                else capability.native_available and not capability.derived
            )
            within_budget = bars <= request.maximum_bar_budget
            redundant = timeframe in existing_full
            degree_compatible = _timeframe_is_allowed_for_request(request, timeframe)
            estimate_available = (
                estimate.estimate_quality is not BarEstimateQuality.UNAVAILABLE
                and bars > 0
            )
            useful = (
                proof_check.viable and not redundant
                if proof_check is not None
                else (
                    lower
                    and native
                    and within_budget
                    and not redundant
                    and degree_compatible
                    and estimate_available
                )
            )
            distance = (
                0
                if request.desired_native_bar_minimum <= bars <= request.desired_native_bar_maximum
                else request.desired_native_bar_minimum - bars
                if bars < request.desired_native_bar_minimum
                else bars - request.desired_native_bar_maximum
            )
            in_target = request.desired_native_bar_minimum <= bars <= request.desired_native_bar_maximum
            score = (
                float(estimate_quality_rank(estimate.estimate_quality)),
                0.0 if in_target else 1.0,
                float(distance),
                float(bars),
                *(
                    ()
                    if root_discovery
                    else (float(timeframe_duration_seconds(timeframe)),)
                ),
            )
            reason = (
                "native useful candidate"
                if useful
                else ",".join(proof_check.reason_codes)
                if proof_check is not None and proof_check.reason_codes
                else "not finer than parent"
                if not lower
                else "not provider-native"
                if not native
                else "incompatible with expected child degree"
                if not degree_compatible
                else "native-bar estimate unavailable"
                if not estimate_available
                else "bar budget exceeded"
                if not within_budget
                else "redundant with existing nearby native coverage"
            )
            assessment = TimeframeCandidateAssessment(
                timeframe=timeframe,
                estimated_native_bars=bars,
                native_available=native,
                useful=useful,
                redundant=redundant,
                score=score,
                reason=reason,
                estimation_basis=estimate.estimation_basis.value,
                market_calendar_id=estimate.market_calendar_id,
                session_policy=estimate.session_policy,
                session_minutes=estimate.session_minutes,
                provider_bar_alignment=estimate.provider_bar_alignment,
                estimate_quality=estimate.estimate_quality,
                assumptions=estimate.assumptions,
                structural_interval_start_utc=(
                    estimate.structural_interval_start_utc
                ),
                structural_interval_end_utc=estimate.structural_interval_end_utc,
            )
            assessments.append(assessment)
            if explicit == timeframe:
                selected_capability = capability if useful else None
                selected_bars = bars

        if explicit is not None:
            capability = capabilities.available(explicit)
            if capability is None or explicit not in request.provider_supported_intervals:
                return self._decision(
                    request,
                    status=TimeframePlanningStatus.UNSUPPORTED,
                    reason=TimeframePlanningReason.PROVIDER_CAPABILITY_UNAVAILABLE,
                    stop_reason="The explicit timeframe is not advertised as native by the provider.",
                    assessments=tuple(assessments),
                    selected=None,
                    provider_alias=None,
                    bars=selected_bars,
                    timestamp=timestamp,
                    proof_feasibility=proof_feasibility,
                )
            if not _timeframe_is_allowed_for_request(request, explicit):
                return self._decision(
                    request,
                    status=TimeframePlanningStatus.UNSUPPORTED,
                    reason=(
                        TimeframePlanningReason.INCOMPATIBLE_WITH_EXPECTED_CHILD_DEGREE
                    ),
                    stop_reason=(
                        "The explicit timeframe is incompatible with the expected "
                        "next-lower Elliott degree."
                    ),
                    assessments=tuple(assessments),
                    selected=None,
                    provider_alias=None,
                    bars=selected_bars,
                    timestamp=timestamp,
                    proof_feasibility=proof_feasibility,
                )
            if selected_capability is None:
                reason = (
                    TimeframePlanningReason.BAR_BUDGET_EXCEEDED
                    if selected_bars is not None and selected_bars > request.maximum_bar_budget
                    else TimeframePlanningReason.REDUNDANT_NEARBY_INTERVAL
                )
                return self._decision(
                    request,
                    status=(
                        TimeframePlanningStatus.BUDGET_EXHAUSTED
                        if proof_feasibility is not None
                        and proof_feasibility.status
                        is ParentProofFeasibilityStatus.BUDGET_EXHAUSTED
                        else TimeframePlanningStatus.CONFIGURATION_ERROR
                        if proof_feasibility is not None
                        and proof_feasibility.status
                        is ParentProofFeasibilityStatus.CONFIGURATION_ERROR
                        else TimeframePlanningStatus.UNRESOLVED
                    ),
                    reason=reason,
                    stop_reason="The explicit interval cannot add bounded non-redundant native evidence.",
                    assessments=tuple(assessments),
                    selected=None,
                    provider_alias=None,
                    bars=selected_bars,
                    timestamp=timestamp,
                    proof_feasibility=proof_feasibility,
                )
            return self._decision(
                request,
                status=TimeframePlanningStatus.PLANNED,
                reason=TimeframePlanningReason.EXPLICIT_SUPPORTED_REQUEST,
                stop_reason="Explicit provider-supported native interval selected.",
                assessments=tuple(assessments),
                selected=explicit,
                provider_alias=selected_capability.provider_alias,
                bars=selected_bars,
                timestamp=timestamp,
                proof_feasibility=proof_feasibility,
            )

        useful = [
            (assessment, capabilities.available(assessment.timeframe))
            for assessment in assessments
            if assessment.useful
        ]
        useful = [(assessment, capability) for assessment, capability in useful if capability is not None]
        if not useful:
            if proof_feasibility is not None:
                status = {
                    ParentProofFeasibilityStatus.NOT_COVERED: TimeframePlanningStatus.NOT_COVERED,
                    ParentProofFeasibilityStatus.BUDGET_EXHAUSTED: TimeframePlanningStatus.BUDGET_EXHAUSTED,
                    ParentProofFeasibilityStatus.CONFIGURATION_ERROR: TimeframePlanningStatus.CONFIGURATION_ERROR,
                }.get(
                    proof_feasibility.status,
                    TimeframePlanningStatus.UNRESOLVED,
                )
                reason = (
                    TimeframePlanningReason.BAR_BUDGET_EXCEEDED
                    if status is TimeframePlanningStatus.BUDGET_EXHAUSTED
                    else TimeframePlanningReason.PROOF_POLICY_CONFIGURATION_ERROR
                    if status is TimeframePlanningStatus.CONFIGURATION_ERROR
                    else TimeframePlanningReason.NO_SUPPORTED_LOWER_INTERVAL
                )
                return self._decision(
                    request,
                    status=status,
                    reason=reason,
                    stop_reason=(
                        "No finer native interval fits the hard bar budget."
                        if status is TimeframePlanningStatus.BUDGET_EXHAUSTED
                        else "Subdivision proof policy or provider metadata is contradictory."
                        if status is TimeframePlanningStatus.CONFIGURATION_ERROR
                        else "The provider advertises no requestable native finer interval for this parent."
                    ),
                    assessments=tuple(assessments),
                    selected=None,
                    provider_alias=None,
                    bars=None,
                    timestamp=timestamp,
                    proof_feasibility=proof_feasibility,
                )
            any_lower_native = any(
                (
                    root_discovery
                    or timeframe_is_finer(
                        item.canonical_name,
                        request.parent_timeframe,
                    )
                )
                and item.native_available
                and not item.derived
                and _timeframe_is_allowed_for_request(request, item.canonical_name)
                for item in capabilities.capabilities
            )
            return self._decision(
                request,
                status=(TimeframePlanningStatus.UNRESOLVED if any_lower_native else TimeframePlanningStatus.NOT_COVERED),
                reason=(TimeframePlanningReason.REDUNDANT_NEARBY_INTERVAL if any_lower_native else TimeframePlanningReason.NO_SUPPORTED_LOWER_INTERVAL),
                stop_reason=(
                    "No non-redundant interval fits the request and bar budgets."
                    if any_lower_native
                    else "The provider advertises no native lower interval for this parent."
                ),
                assessments=tuple(assessments),
                selected=None,
                provider_alias=None,
                bars=None,
                timestamp=timestamp,
                proof_feasibility=proof_feasibility,
            )
        assessment, capability = min(useful, key=lambda item: item[0].score)
        replan = request.reason_code == TimeframePlanningReason.ROOT_TIMEFRAME_REPLAN.value
        return self._decision(
            request,
            status=TimeframePlanningStatus.PLANNED,
            reason=(
                TimeframePlanningReason.ROOT_TIMEFRAME_REPLAN
                if replan
                else TimeframePlanningReason.TARGET_BAR_RANGE
            ),
            stop_reason=(
                "Selected a bounded replacement root timeframe after an actual-bar "
                "count materially missed the focused range."
                if replan
                else f"Selected the least-expensive useful native interval nearest the "
                f"{request.desired_native_bar_minimum}-{request.desired_native_bar_maximum} bar view."
            ),
            assessments=tuple(assessments),
            selected=assessment.timeframe,
            provider_alias=capability.provider_alias,
            bars=assessment.estimated_native_bars,
            timestamp=timestamp,
            proof_feasibility=proof_feasibility,
        )

    @staticmethod
    def _near_existing(timeframe: str, existing: set[str]) -> bool:
        duration = timeframe_duration_seconds(timeframe)
        return any(
            max(duration, timeframe_duration_seconds(item))
            / min(duration, timeframe_duration_seconds(item))
            < 1.5
            for item in existing
        )

    @staticmethod
    def _decision(
        request: AdaptiveTimeframeRequest,
        *,
        status: TimeframePlanningStatus,
        reason: TimeframePlanningReason,
        stop_reason: str,
        assessments: tuple[TimeframeCandidateAssessment, ...],
        selected: str | None,
        provider_alias: str | None,
        bars: int | None,
        timestamp: str,
        increment: bool = True,
        proof_feasibility: ParentProofFeasibility | None = None,
    ) -> AdaptiveTimeframeDecision:
        return AdaptiveTimeframeDecision.create(
            decision_id="",
            request_id=request.request_id,
            request_content_hash=request.content_hash,
            status=status,
            selected_timeframe=selected,
            provider_alias=provider_alias,
            estimated_native_bars=bars,
            reason_code=reason,
            stop_reason=stop_reason,
            assessments=assessments,
            requests_after_decision=(request.requests_already_used + (1 if status is TimeframePlanningStatus.PLANNED and increment else 0)),
            created_at_utc=timestamp,
            proof_feasibility=proof_feasibility,
            schema_version=(
                ROOT_TIMEFRAME_DECISION_SCHEMA_VERSION
                if request.structural_question
                is StructuralQuestion.DISCOVER_ROOT_STRUCTURE
                else TIMEFRAME_DECISION_SCHEMA_VERSION
            ),
            policy_version=request.policy_version,
        )


__all__ = [
    "ADAPTIVE_TIMEFRAME_PLANNER_POLICY_VERSION",
    "ADAPTIVE_TIMEFRAME_PLANNER_SCHEMA_VERSION",
    "CHILD_PROOF_RESOLUTION_CHECK_SCHEMA_VERSION",
    "PARENT_PROOF_FEASIBILITY_SCHEMA_VERSION",
    "ROOT_TIMEFRAME_DECISION_SCHEMA_VERSION",
    "ROOT_TIMEFRAME_PLANNER_POLICY_VERSION",
    "AdaptiveTimeframeDecision",
    "AdaptiveTimeframePlanner",
    "AdaptiveTimeframeRequest",
    "ChildProofResolutionCheck",
    "ParentProofFeasibility",
    "ParentProofFeasibilityStatus",
    "StructuralQuestion",
    "TimeframeCandidateAssessment",
    "TimeframeEvidenceCoverage",
    "TimeframePlanningReason",
    "TimeframePlanningStatus",
    "adaptive_timeframe_decision_content_hash",
    "adaptive_timeframe_request_content_hash",
    "assess_parent_proof_feasibility",
    "parent_proof_feasibility_content_hash",
    "timeframe_can_resolve_child_structure",
]
