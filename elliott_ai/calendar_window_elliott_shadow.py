"""Opt-in calendar-window preflight for bounded Elliott shadow analysis.

This module deliberately separates a calendar observation window from an
Elliott structural origin.  A calendar boundary can provide useful context,
but it cannot become Wave 1, Wave A, or any other structural pivot merely
because it is a convenient date.  The initial policy targets the final twelve
completed GOOGL Monthly candles available in the immutable full-history
bundle.  It retains the earlier portion as context and delegates all actual
candidate-map preparation to the existing catalog-pivot-scoped runner.

The module is in-memory and opt-in.  Preflight is provider-free and makes no
model call.  Its separately approved execution bridge can call the existing
pivot-scoped runner while preserving the calendar context outside every wave
catalog.  It writes no artifacts or database rows and does not change the
Complete-History or pivot-scoped shadow runners.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .complete_history_shadow_runner import (
    CompleteHistoryBundle,
    CompleteHistoryCandidateProvider,
    CompleteHistoryDataset,
    complete_history_bundle_content_hash,
    load_complete_history_bundle,
)
from .forecast_records import canonical_sha256
from .lower_timeframe_candidate_generator import (
    NativeOHLCVWindow,
    PivotPriceField,
    TypedPivot,
    WindowCoverageRole,
    build_native_pivot_catalog,
)
from .scoped_elliott_shadow_runner import (
    GOOGL_SCOPED_EXCHANGE,
    GOOGL_SCOPED_SYMBOL,
    SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES,
    SCOPED_REQUIRED_PROOF_TIMEFRAMES,
    ScopedElliottShadowPreflight,
    ScopedElliottShadowApproval,
    ScopedElliottShadowExecution,
    ScopedElliottShadowError,
    ScopedElliottShadowRunner,
    ScopedTimeframeCoverage,
    ScopedCoverageStatus,
    build_scoped_elliott_shadow_preflight_from_bundle,
)


CALENDAR_WINDOW_ELLIOTT_SHADOW_SCHEMA_VERSION = "calendar-window-elliott-shadow-1.1.0"
CALENDAR_WINDOW_SCOPE_SCHEMA_VERSION = "calendar-window-elliott-shadow-scope-1.0.0"
CALENDAR_WINDOW_PLAN_SCHEMA_VERSION = "calendar-window-elliott-shadow-plan-1.0.0"
CALENDAR_WINDOW_APPROVAL_SCHEMA_VERSION = "calendar-window-elliott-shadow-approval-1.0.0"
CALENDAR_WINDOW_COMPLETED_MONTHS = 12
CALENDAR_WINDOW_STAGE1_CALL_BUDGET = 4

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_TIMEFRAMES = (*SCOPED_REQUIRED_PROOF_TIMEFRAMES, *SCOPED_OPTIONAL_EVIDENCE_TIMEFRAMES)
_EPSILON = 1e-9


class CalendarWindowElliottShadowError(RuntimeError):
    """Raised when a calendar window cannot preserve structural boundaries."""


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


def _json_value(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Canonical JSON cannot contain non-finite values.")
        return value
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


def _contract_hash(value: Any) -> str:
    payload = value.to_dict()
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _coverage(
    dataset: CompleteHistoryDataset,
    *,
    start_utc: str,
    end_utc: str,
    required_for_proof: bool,
) -> ScopedTimeframeCoverage:
    complete = (
        _utc_datetime(dataset.coverage_start_utc) <= _utc_datetime(start_utc)
        and _utc_datetime(dataset.coverage_end_utc) >= _utc_datetime(end_utc)
    )
    warnings: tuple[str, ...]
    if complete:
        status = ScopedCoverageStatus.COMPLETE
        warnings = ()
    elif required_for_proof:
        status = ScopedCoverageStatus.NOT_COVERED
        warnings = ("Required proof timeframe does not fully cover the calendar observation window.",)
    elif (
        _utc_datetime(dataset.coverage_end_utc) < _utc_datetime(start_utc)
        or _utc_datetime(dataset.coverage_start_utc) > _utc_datetime(end_utc)
    ):
        status = ScopedCoverageStatus.NOT_COVERED
        warnings = ("Optional evidence timeframe has no overlap with the calendar observation window.",)
    else:
        status = ScopedCoverageStatus.PARTIAL_OPTIONAL
        warnings = ("Optional evidence timeframe is partial and cannot define or verify a wave.",)
    return ScopedTimeframeCoverage.create(
        timeframe=dataset.timeframe,
        required_for_proof=required_for_proof,
        scope_start_utc=start_utc,
        scope_end_utc=end_utc,
        available_start_utc=dataset.coverage_start_utc,
        available_end_utc=dataset.coverage_end_utc,
        status=status,
        source_dataset_hash=dataset.content_hash,
        warnings=warnings,
    )


def _row_index(window: NativeOHLCVWindow, pivot: TypedPivot) -> int:
    for index, candle in enumerate(window.candles):
        if candle.timestamp_utc == pivot.timestamp_utc and candle.source_row_hash == pivot.source_bar_hash:
            return index
    raise CalendarWindowElliottShadowError("The selected Monthly catalog pivot does not map to its immutable source row.")


def _calendar_monthly_window(
    monthly: CompleteHistoryDataset,
    *,
    completed_months: int,
    window_id: str,
) -> tuple[NativeOHLCVWindow, int, int]:
    if isinstance(completed_months, bool) or not isinstance(completed_months, int) or completed_months < 2:
        raise ValueError("completed_months must be an integer of at least two.")
    rows = monthly.source_window.candles
    if len(rows) < completed_months:
        raise CalendarWindowElliottShadowError("The immutable Monthly dataset does not contain the requested completed calendar window.")
    start_index = len(rows) - completed_months
    end_index = len(rows) - 1
    return (
        NativeOHLCVWindow.create(
            window_id=window_id,
            dataset_cutoff=monthly.proof_dataset_cutoff,
            timeframe="monthly",
            candles=rows[start_index : end_index + 1],
            coverage_role=WindowCoverageRole.SUPPORTING_ONLY,
            is_native=True,
        ),
        start_index,
        end_index,
    )


def _first_internal_monthly_pivot(
    monthly: CompleteHistoryDataset,
    *,
    start_index: int,
    end_index: int,
) -> TypedPivot:
    catalog = build_native_pivot_catalog(monthly.source_window, pivot_window=1)
    indexed: list[tuple[int, TypedPivot]] = []
    for pivot in catalog:
        row_index = _row_index(monthly.source_window, pivot)
        if start_index < row_index < end_index:
            indexed.append((row_index, pivot))
    if not indexed:
        raise CalendarWindowElliottShadowError(
            "The calendar observation window has no confirmed internal Monthly catalog pivot; it cannot establish a structural origin."
        )
    first_index = min(item[0] for item in indexed)
    candidates = tuple(item[1] for item in indexed if item[0] == first_index)
    if len(candidates) != 1:
        raise CalendarWindowElliottShadowError(
            "The first internal Monthly pivot is ambiguous; the calendar window cannot choose a structural origin automatically."
        )
    return candidates[0]


@dataclass(frozen=True, slots=True)
class CalendarWindowElliottScope(_JsonContract):
    """Immutable observation/context boundary around a pivot-scoped analysis."""

    scope_id: str
    symbol: str
    exchange: str
    source_bundle_content_hash: str
    analysis_cutoff_utc: str
    completed_months: int
    calendar_monthly_start_utc: str
    calendar_monthly_end_utc: str
    calendar_monthly_window_hash: str
    calendar_as_of_monthly_candle_id: str
    context_start_utc: str
    context_end_exclusive_utc: str
    context_monthly_window_hash: str
    context_candle_ids: tuple[str, ...]
    first_structural_source_pivot: TypedPivot
    first_structural_scoped_pivot: TypedPivot
    structural_scoped_scope_content_hash: str
    coverage: tuple[ScopedTimeframeCoverage, ...]
    schema_version: str = CALENDAR_WINDOW_SCOPE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("scope_id", "symbol", "exchange", "calendar_as_of_monthly_candle_id", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in (
            "source_bundle_content_hash",
            "calendar_monthly_window_hash",
            "context_monthly_window_hash",
            "structural_scoped_scope_content_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if isinstance(self.completed_months, bool) or not isinstance(self.completed_months, int) or self.completed_months < 2:
            raise ValueError("completed_months must be an integer of at least two.")
        for name in (
            "analysis_cutoff_utc",
            "calendar_monthly_start_utc",
            "calendar_monthly_end_utc",
            "context_start_utc",
            "context_end_exclusive_utc",
        ):
            object.__setattr__(self, name, _utc(getattr(self, name), field_name=name))
        if self.context_start_utc != self.calendar_monthly_start_utc:
            raise ValueError("Calendar context must begin at the exact calendar observation start.")
        if not (
            _utc_datetime(self.calendar_monthly_start_utc)
            < _utc_datetime(self.context_end_exclusive_utc)
            <= _utc_datetime(self.calendar_monthly_end_utc)
            <= _utc_datetime(self.analysis_cutoff_utc)
        ):
            raise ValueError("Calendar, context, structural, and cutoff boundaries are inconsistent.")
        if not isinstance(self.first_structural_source_pivot, TypedPivot) or not isinstance(self.first_structural_scoped_pivot, TypedPivot):
            raise TypeError("Calendar structural origins must be typed Monthly pivots.")
        source = self.first_structural_source_pivot
        scoped = self.first_structural_scoped_pivot
        if source.timeframe != "monthly" or scoped.timeframe != "monthly":
            raise ValueError("Calendar structural origins must be Monthly pivots.")
        if source.timestamp_utc != self.context_end_exclusive_utc or scoped.timestamp_utc != self.context_end_exclusive_utc:
            raise ValueError("The first structural pivot must begin exactly after the context interval.")
        if (
            source.price_field is not scoped.price_field
            or abs(source.price - scoped.price) > _EPSILON
            or source.source_bar_hash != scoped.source_bar_hash
        ):
            raise ValueError("The scoped structural pivot must preserve the exact selected source pivot.")
        if isinstance(self.context_candle_ids, (str, bytes)) or not isinstance(self.context_candle_ids, Sequence):
            raise TypeError("context_candle_ids must be a sequence.")
        context_ids = tuple(_text(item, field_name="context_candle_ids") for item in self.context_candle_ids)
        if not context_ids or len(context_ids) != len(set(context_ids)):
            raise ValueError("Calendar context requires unique inherited candle IDs.")
        object.__setattr__(self, "context_candle_ids", context_ids)
        if isinstance(self.coverage, (str, bytes)) or not isinstance(self.coverage, Sequence):
            raise TypeError("coverage must be a sequence.")
        coverage = tuple(self.coverage)
        if not all(isinstance(item, ScopedTimeframeCoverage) for item in coverage):
            raise TypeError("coverage must contain immutable timeframe coverage facts.")
        if tuple(item.timeframe for item in coverage) != _TIMEFRAMES:
            raise ValueError("Coverage must contain the required and optional timeframes in canonical order.")
        if any(
            item.scope_start_utc != self.calendar_monthly_start_utc or item.scope_end_utc != self.calendar_monthly_end_utc
            for item in coverage
        ):
            raise ValueError("Calendar coverage must use the full observation window, not only the structural subwindow.")
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != calendar_window_scope_content_hash(self):
            raise ValueError("CalendarWindowElliottScope content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CalendarWindowElliottScope":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("scope_id"):
            values["scope_id"] = "calendar_window_scope_" + canonical_sha256(
                {
                    "bundle": values.get("source_bundle_content_hash"),
                    "calendar_start": values.get("calendar_monthly_start_utc"),
                    "calendar_end": values.get("calendar_monthly_end_utc"),
                    "structural_pivot": _json_value(values.get("first_structural_source_pivot")),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=calendar_window_scope_content_hash(result))


def calendar_window_scope_content_hash(value: CalendarWindowElliottScope | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CalendarWindowElliottScope) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CalendarWindowElliottPlan(_JsonContract):
    """Immutable Stage-1 reservation bound to the full calendar context."""

    plan_id: str
    bundle_content_hash: str
    calendar_scope: CalendarWindowElliottScope
    structural_scoped_plan_content_hash: str
    rules_pack_content_hash: str
    total_reserved_model_calls: int
    requires_human_approval: bool
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = CALENDAR_WINDOW_PLAN_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("plan_id", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in ("bundle_content_hash", "structural_scoped_plan_content_hash", "rules_pack_content_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if not isinstance(self.calendar_scope, CalendarWindowElliottScope):
            raise TypeError("calendar_scope must be a CalendarWindowElliottScope.")
        if self.bundle_content_hash != self.calendar_scope.source_bundle_content_hash:
            raise ValueError("Calendar plan and scope must bind the same source bundle.")
        if self.total_reserved_model_calls != CALENDAR_WINDOW_STAGE1_CALL_BUDGET:
            raise ValueError("Calendar Stage 1 must reserve exactly four possible model calls.")
        if not isinstance(self.requires_human_approval, bool) or not self.requires_human_approval:
            raise ValueError("Calendar Stage 1 must require explicit human approval.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        object.__setattr__(self, "warnings", tuple(_text(item, field_name="warnings") for item in self.warnings))
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != calendar_window_plan_content_hash(self):
            raise ValueError("CalendarWindowElliottPlan content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CalendarWindowElliottPlan":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("plan_id"):
            values["plan_id"] = "calendar_window_plan_" + canonical_sha256(
                {
                    "bundle": values.get("bundle_content_hash"),
                    "calendar_scope": _json_value(values.get("calendar_scope")),
                    "structural_plan": values.get("structural_scoped_plan_content_hash"),
                    "created": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=calendar_window_plan_content_hash(result))


def calendar_window_plan_content_hash(value: CalendarWindowElliottPlan | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CalendarWindowElliottPlan) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CalendarWindowElliottApproval(_JsonContract):
    """Future execution approval bound to both context and structural plans."""

    approval_id: str
    calendar_plan_content_hash: str
    bundle_content_hash: str
    calendar_scope_content_hash: str
    structural_scoped_plan_content_hash: str
    structural_start_pivot_content_hash: str
    rules_pack_content_hash: str
    approved_by: str
    approved_at_utc: str
    exact_call_limit: int
    provider: str
    model: str
    purpose: str = "calendar_window_elliott_shadow_model_calls"
    schema_version: str = CALENDAR_WINDOW_APPROVAL_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("approval_id", "approved_by", "provider", "model", "purpose", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if self.purpose != "calendar_window_elliott_shadow_model_calls":
            raise ValueError("Approval purpose is not authorized for calendar-window shadow calls.")
        for name in (
            "calendar_plan_content_hash",
            "bundle_content_hash",
            "calendar_scope_content_hash",
            "structural_scoped_plan_content_hash",
            "structural_start_pivot_content_hash",
            "rules_pack_content_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "approved_at_utc", _utc(self.approved_at_utc, field_name="approved_at_utc"))
        if self.exact_call_limit != CALENDAR_WINDOW_STAGE1_CALL_BUDGET:
            raise ValueError("Calendar approval must authorize the exact four-call Stage-1 ceiling.")
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != calendar_window_approval_content_hash(self):
            raise ValueError("CalendarWindowElliottApproval content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CalendarWindowElliottApproval":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("approval_id"):
            values["approval_id"] = "calendar_window_approval_" + canonical_sha256(
                {
                    "plan": values.get("calendar_plan_content_hash"),
                    "scope": values.get("calendar_scope_content_hash"),
                    "structural_pivot": values.get("structural_start_pivot_content_hash"),
                    "approved_by": values.get("approved_by"),
                    "approved_at": values.get("approved_at_utc"),
                    "provider": values.get("provider"),
                    "model": values.get("model"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=calendar_window_approval_content_hash(result))


def calendar_window_approval_content_hash(value: CalendarWindowElliottApproval | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CalendarWindowElliottApproval) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CalendarWindowElliottExecution(_JsonContract):
    """In-memory execution preserving calendar context outside candidate waves."""

    execution_id: str
    calendar_plan_content_hash: str
    calendar_scope_content_hash: str
    calendar_approval_content_hash: str
    structural_execution: ScopedElliottShadowExecution
    warnings: tuple[str, ...]
    created_at_utc: str
    schema_version: str = CALENDAR_WINDOW_ELLIOTT_SHADOW_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("execution_id", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in ("calendar_plan_content_hash", "calendar_scope_content_hash", "calendar_approval_content_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if not isinstance(self.structural_execution, ScopedElliottShadowExecution):
            raise TypeError("structural_execution must be the immutable pivot-scoped execution result.")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(self.warnings, Sequence):
            raise TypeError("warnings must be a sequence.")
        object.__setattr__(self, "warnings", tuple(_text(item, field_name="warnings") for item in self.warnings))
        object.__setattr__(self, "created_at_utc", _utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != calendar_window_execution_content_hash(self):
            raise ValueError("CalendarWindowElliottExecution content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "CalendarWindowElliottExecution":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("execution_id"):
            values["execution_id"] = "calendar_window_execution_" + canonical_sha256(
                {
                    "calendar_plan": values.get("calendar_plan_content_hash"),
                    "calendar_approval": values.get("calendar_approval_content_hash"),
                    "structural_execution": _json_value(values.get("structural_execution")),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=calendar_window_execution_content_hash(result))


def calendar_window_execution_content_hash(value: CalendarWindowElliottExecution | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, CalendarWindowElliottExecution) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class CalendarWindowElliottPreflight:
    """Read-only calendar observation window plus its structural subwindow."""

    bundle: CompleteHistoryBundle
    calendar_monthly_window: NativeOHLCVWindow
    context_monthly_window: NativeOHLCVWindow
    structural_preflight: ScopedElliottShadowPreflight
    calendar_scope: CalendarWindowElliottScope
    plan: CalendarWindowElliottPlan


def build_calendar_window_elliott_preflight_from_bundle(
    bundle: CompleteHistoryBundle,
    *,
    completed_months: int = CALENDAR_WINDOW_COMPLETED_MONTHS,
    symbol: str = GOOGL_SCOPED_SYMBOL,
    exchange: str = GOOGL_SCOPED_EXCHANGE,
    created_at_utc: str | None = None,
) -> CalendarWindowElliottPreflight:
    """Build a no-model plan that keeps inherited calendar context non-structural."""

    if not isinstance(bundle, CompleteHistoryBundle):
        raise TypeError("bundle must be a CompleteHistoryBundle.")
    if bundle.content_hash != complete_history_bundle_content_hash(bundle):
        raise CalendarWindowElliottShadowError("The immutable full-history bundle no longer reproduces its content hash.")
    monthly = bundle.dataset("monthly")
    if monthly.proof_dataset_cutoff.resolved_symbol != symbol or monthly.proof_dataset_cutoff.exchange != exchange:
        raise CalendarWindowElliottShadowError("Calendar-window symbol or exchange does not match the immutable Monthly dataset.")
    calendar_window, start_index, end_index = _calendar_monthly_window(
        monthly,
        completed_months=completed_months,
        window_id=f"calendar_window:{bundle.bundle_id}:monthly:{completed_months}",
    )
    source_pivot = _first_internal_monthly_pivot(monthly, start_index=start_index, end_index=end_index)
    structural_index = _row_index(monthly.source_window, source_pivot)
    context_rows = monthly.source_window.candles[start_index:structural_index]
    if not context_rows:
        raise CalendarWindowElliottShadowError("The first internal pivot cannot be identical to the calendar observation boundary.")
    context_window = NativeOHLCVWindow.create(
        window_id=f"calendar_window:{bundle.bundle_id}:monthly:context:{start_index}:{structural_index - 1}",
        dataset_cutoff=monthly.proof_dataset_cutoff,
        timeframe="monthly",
        candles=context_rows,
        coverage_role=WindowCoverageRole.SUPPORTING_ONLY,
        is_native=True,
    )
    structural_preflight = build_scoped_elliott_shadow_preflight_from_bundle(
        bundle,
        start_timestamp_utc=source_pivot.timestamp_utc,
        start_price=source_pivot.price,
        start_price_field=PivotPriceField(source_pivot.price_field),
        symbol=symbol,
        exchange=exchange,
        created_at_utc=created_at_utc or bundle.shared_cutoff_utc,
    )
    scoped_pivot = structural_preflight.scope.scoped_selected_start_pivot
    if (
        source_pivot.source_bar_hash != scoped_pivot.source_bar_hash
        or source_pivot.price_field is not scoped_pivot.price_field
        or abs(source_pivot.price - scoped_pivot.price) > _EPSILON
    ):
        raise CalendarWindowElliottShadowError("The structural subwindow did not retain the selected first internal catalog pivot.")
    coverage = tuple(
        _coverage(
            bundle.dataset(timeframe),
            start_utc=calendar_window.candles[0].timestamp_utc,
            end_utc=calendar_window.candles[-1].timestamp_utc,
            required_for_proof=timeframe in SCOPED_REQUIRED_PROOF_TIMEFRAMES,
        )
        for timeframe in _TIMEFRAMES
    )
    scope = CalendarWindowElliottScope.create(
        scope_id="",
        symbol=symbol,
        exchange=exchange,
        source_bundle_content_hash=bundle.content_hash,
        analysis_cutoff_utc=bundle.shared_cutoff_utc,
        completed_months=completed_months,
        calendar_monthly_start_utc=calendar_window.candles[0].timestamp_utc,
        calendar_monthly_end_utc=calendar_window.candles[-1].timestamp_utc,
        calendar_monthly_window_hash=calendar_window.native_rows_hash,
        calendar_as_of_monthly_candle_id=calendar_window.candles[-1].candle_id,
        context_start_utc=context_window.candles[0].timestamp_utc,
        context_end_exclusive_utc=source_pivot.timestamp_utc,
        context_monthly_window_hash=context_window.native_rows_hash,
        context_candle_ids=tuple(item.candle_id for item in context_window.candles),
        first_structural_source_pivot=source_pivot,
        first_structural_scoped_pivot=scoped_pivot,
        structural_scoped_scope_content_hash=structural_preflight.scope.content_hash,
        coverage=coverage,
    )
    plan = CalendarWindowElliottPlan.create(
        plan_id="",
        bundle_content_hash=bundle.content_hash,
        calendar_scope=scope,
        structural_scoped_plan_content_hash=structural_preflight.plan.content_hash,
        rules_pack_content_hash=structural_preflight.plan.policy.blind_candidate_rules_pack.content_hash,
        total_reserved_model_calls=CALENDAR_WINDOW_STAGE1_CALL_BUDGET,
        requires_human_approval=True,
        warnings=(
            "The calendar start is context only and cannot be used as a wave origin, pivot, or candidate label.",
            "The first confirmed internal Monthly catalog pivot is the sole structural origin for Primary and Alternative candidate maps.",
            "Monthly, Weekly, Daily, and native 4h coverage are assessed over the full calendar observation window; 1h and 15m remain optional partial evidence.",
            "Stage 1 reserves at most four calls: independent Primary and Alternative maps followed by one eligible Weekly batch per role.",
            "All model candidates remain unproven until the existing deterministic Monthly -> Weekly -> Daily -> native 4h proof ladder succeeds.",
        ),
        created_at_utc=created_at_utc or bundle.shared_cutoff_utc,
    )
    return CalendarWindowElliottPreflight(
        bundle=bundle,
        calendar_monthly_window=calendar_window,
        context_monthly_window=context_window,
        structural_preflight=structural_preflight,
        calendar_scope=scope,
        plan=plan,
    )


def build_googl_last_twelve_month_calendar_preflight(
    bundle_directory: str | Path,
    *,
    created_at_utc: str | None = None,
) -> CalendarWindowElliottPreflight:
    """Build the initial GOOGL August-2025 through July-2026 calendar plan."""

    return build_calendar_window_elliott_preflight_from_bundle(
        load_complete_history_bundle(bundle_directory),
        completed_months=CALENDAR_WINDOW_COMPLETED_MONTHS,
        symbol=GOOGL_SCOPED_SYMBOL,
        exchange=GOOGL_SCOPED_EXCHANGE,
        created_at_utc=created_at_utc,
    )


class _CalendarContextBoundCandidateProvider:
    """Adds immutable context without expanding the model's wave-pivot catalog."""

    def __init__(self, provider: CompleteHistoryCandidateProvider, preflight: CalendarWindowElliottPreflight) -> None:
        name = getattr(provider, "name", None)
        model = getattr(provider, "model", None)
        if not isinstance(name, str) or not name.strip() or not isinstance(model, str) or not model.strip():
            raise TypeError("Calendar execution requires a provider with non-empty name and model attributes.")
        self._provider = provider
        self._preflight = preflight
        self.name = name
        self.model = model

    def _packet(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, Mapping):
            raise TypeError("Candidate packet must be a mapping.")
        scope = self._preflight.calendar_scope
        result = dict(packet)
        result["calendar_window_context"] = {
            "classification": "context_before_first_confirmed_pivot",
            "calendar_plan_content_hash": self._preflight.plan.content_hash,
            "calendar_scope_content_hash": scope.content_hash,
            "calendar_monthly_start_utc": scope.calendar_monthly_start_utc,
            "calendar_monthly_end_utc": scope.calendar_monthly_end_utc,
            "context_start_utc": scope.context_start_utc,
            "context_end_exclusive_utc": scope.context_end_exclusive_utc,
            "context_monthly_window_hash": scope.context_monthly_window_hash,
            "context_candles": [item.to_dict() for item in self._preflight.context_monthly_window.candles],
            "first_structural_pivot": scope.first_structural_scoped_pivot.to_dict(),
            "instructions": (
                "The context candles are inherited observation context only.",
                "Do not label, relabel, or use them as a candidate-wave origin.",
                "Only the supplied structural pivot catalog may be referenced by candidate wave or invalidation fields.",
            ),
        }
        return result

    def generate_monthly_map(
        self,
        *,
        role: Any,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._provider.generate_monthly_map(
            role=role,
            packet=self._packet(packet),
            output_schema=output_schema,
        )

    def generate_weekly_child_graph_batch(
        self,
        *,
        role: Any,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._provider.generate_weekly_child_graph_batch(
            role=role,
            packet=self._packet(packet),
            output_schema=output_schema,
        )


class CalendarWindowElliottShadowRunner:
    """Manual calendar-context coordinator around the existing scoped runner."""

    def preflight(
        self,
        bundle_directory: str | Path,
        *,
        created_at_utc: str | None = None,
    ) -> CalendarWindowElliottPreflight:
        return build_googl_last_twelve_month_calendar_preflight(
            bundle_directory,
            created_at_utc=created_at_utc,
        )

    @staticmethod
    def approval_template(
        preflight: CalendarWindowElliottPreflight,
        *,
        approved_by: str,
        approved_at_utc: str,
        provider: str,
        model: str,
    ) -> CalendarWindowElliottApproval:
        """Create a caller-owned, no-model approval for a future execution phase."""

        return CalendarWindowElliottApproval.create(
            approval_id="",
            calendar_plan_content_hash=preflight.plan.content_hash,
            bundle_content_hash=preflight.bundle.content_hash,
            calendar_scope_content_hash=preflight.calendar_scope.content_hash,
            structural_scoped_plan_content_hash=preflight.structural_preflight.plan.content_hash,
            structural_start_pivot_content_hash=preflight.calendar_scope.first_structural_scoped_pivot.content_hash,
            rules_pack_content_hash=preflight.plan.rules_pack_content_hash,
            approved_by=approved_by,
            approved_at_utc=approved_at_utc,
            exact_call_limit=preflight.plan.total_reserved_model_calls,
            provider=provider,
            model=model,
        )

    def execute(
        self,
        preflight: CalendarWindowElliottPreflight,
        *,
        provider: CompleteHistoryCandidateProvider,
        approval: CalendarWindowElliottApproval | None,
        allow_model_calls: bool = False,
        created_at_utc: str | None = None,
    ) -> CalendarWindowElliottExecution:
        """Run the existing scoped Stage 1 with context held outside wave labels."""

        if not isinstance(preflight, CalendarWindowElliottPreflight):
            raise TypeError("preflight must be a CalendarWindowElliottPreflight.")
        if preflight.bundle.content_hash != complete_history_bundle_content_hash(preflight.bundle):
            raise CalendarWindowElliottShadowError("The calendar preflight bundle no longer reproduces its immutable hash.")
        if preflight.calendar_scope.content_hash != calendar_window_scope_content_hash(preflight.calendar_scope):
            raise CalendarWindowElliottShadowError("The calendar scope no longer reproduces its canonical hash.")
        if preflight.plan.content_hash != calendar_window_plan_content_hash(preflight.plan):
            raise CalendarWindowElliottShadowError("The calendar plan no longer reproduces its canonical hash.")
        if not allow_model_calls:
            raise CalendarWindowElliottShadowError("allow_model_calls=True is required before any calendar-window candidate request.")
        if approval is None:
            raise CalendarWindowElliottShadowError("A separately generated calendar-window approval is required before model calls.")
        if approval.content_hash != calendar_window_approval_content_hash(approval):
            raise CalendarWindowElliottShadowError("The calendar approval no longer reproduces its canonical hash.")
        scope = preflight.calendar_scope
        if (
            approval.calendar_plan_content_hash != preflight.plan.content_hash
            or approval.bundle_content_hash != preflight.bundle.content_hash
            or approval.calendar_scope_content_hash != scope.content_hash
            or approval.structural_scoped_plan_content_hash != preflight.structural_preflight.plan.content_hash
            or approval.structural_start_pivot_content_hash != scope.first_structural_scoped_pivot.content_hash
            or approval.rules_pack_content_hash != preflight.plan.rules_pack_content_hash
            or approval.exact_call_limit != preflight.plan.total_reserved_model_calls
        ):
            raise CalendarWindowElliottShadowError("The calendar approval does not bind the exact observation context and structural subwindow.")
        provider_name = getattr(provider, "name", None)
        provider_model = getattr(provider, "model", None)
        if provider_name != approval.provider or provider_model != approval.model:
            raise CalendarWindowElliottShadowError("The supplied provider or model does not match the immutable calendar approval.")
        timestamp = _utc(created_at_utc or approval.approved_at_utc, field_name="created_at_utc")
        structural_approval = ScopedElliottShadowApproval.create(
            approval_id=f"{approval.approval_id}:structural",
            plan_content_hash=preflight.structural_preflight.plan.content_hash,
            bundle_content_hash=preflight.bundle.content_hash,
            scope_content_hash=preflight.structural_preflight.scope.content_hash,
            selected_start_pivot_content_hash=scope.first_structural_scoped_pivot.content_hash,
            rules_pack_content_hash=preflight.plan.rules_pack_content_hash,
            approved_by=approval.approved_by,
            approved_at_utc=approval.approved_at_utc,
            exact_call_limit=preflight.plan.total_reserved_model_calls,
            provider=approval.provider,
            model=approval.model,
        )
        try:
            structural_execution = ScopedElliottShadowRunner().execute(
                preflight.structural_preflight,
                provider=_CalendarContextBoundCandidateProvider(provider, preflight),
                approval=structural_approval,
                allow_model_calls=True,
                created_at_utc=timestamp,
            )
        except ScopedElliottShadowError as exc:
            raise CalendarWindowElliottShadowError(
                "Calendar-window execution stopped because its structurally scoped candidate stage failed: " + str(exc)
            ) from exc
        return CalendarWindowElliottExecution.create(
            execution_id="",
            calendar_plan_content_hash=preflight.plan.content_hash,
            calendar_scope_content_hash=scope.content_hash,
            calendar_approval_content_hash=approval.content_hash,
            structural_execution=structural_execution,
            warnings=(
                "Calendar context was supplied as inherited observation context only; its candles were not included in the candidate pivot catalog.",
                "All candidate-wave and verification statuses originate from the unchanged pivot-scoped runner and its deterministic proof system.",
                "No SQLite, artifact, forecast, resolution, market-data, or active-pipeline mutation occurred.",
            ),
            created_at_utc=timestamp,
        )


__all__ = [
    "CALENDAR_WINDOW_APPROVAL_SCHEMA_VERSION",
    "CALENDAR_WINDOW_COMPLETED_MONTHS",
    "CALENDAR_WINDOW_ELLIOTT_SHADOW_SCHEMA_VERSION",
    "CALENDAR_WINDOW_PLAN_SCHEMA_VERSION",
    "CALENDAR_WINDOW_SCOPE_SCHEMA_VERSION",
    "CALENDAR_WINDOW_STAGE1_CALL_BUDGET",
    "CalendarWindowElliottApproval",
    "CalendarWindowElliottExecution",
    "CalendarWindowElliottPlan",
    "CalendarWindowElliottPreflight",
    "CalendarWindowElliottScope",
    "CalendarWindowElliottShadowError",
    "CalendarWindowElliottShadowRunner",
    "build_calendar_window_elliott_preflight_from_bundle",
    "build_googl_last_twelve_month_calendar_preflight",
    "calendar_window_approval_content_hash",
    "calendar_window_execution_content_hash",
    "calendar_window_plan_content_hash",
    "calendar_window_scope_content_hash",
]
