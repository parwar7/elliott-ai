"""Immutable Model-B market-data requests and native OHLCV bundles.

The Python process does not pretend to be a TradingView MCP client.  It emits a
typed request that an authorized Codex workflow may satisfy with a real raw-
candle MCP tool, then validates the returned immutable bundle before analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adaptive_timeframe_planner import (
    AdaptiveTimeframeDecision,
    AdaptiveTimeframeRequest,
    StructuralQuestion,
    TimeframePlanningStatus,
)
from .forecast_records import canonical_forecast_json, canonical_sha256
from .lower_timeframe_candidate_generator import (
    NativeCandle,
    native_candle_content_hash,
)
from .market_data_identity import (
    MarketDataFeedFamily,
    MarketDataStreamIdentity,
    build_feed_family,
    build_stream_identity,
    canonical_bar_alignment,
    twelve_data_legacy_stream_identity,
)
from .timeframes import (
    ProviderCapabilitySet,
    normalize_timeframe_name,
    provider_capability_set_content_hash,
    timeframe_duration_seconds,
)


DATA_REQUEST_MANIFEST_SCHEMA_VERSION = "adaptive-data-request-manifest-1.4.0"
LEGACY_BOUND_DATA_REQUEST_MANIFEST_SCHEMA_VERSION = "adaptive-data-request-manifest-1.3.0"
ROOT_DATA_REQUEST_MANIFEST_SCHEMA_VERSION = "adaptive-data-request-manifest-1.2.0"
_MANIFEST_SCHEMA_VERSIONS_WITHOUT_STRUCTURAL_WINDOW = frozenset(
    {
        "adaptive-data-request-manifest-1.0.0",
        "adaptive-data-request-manifest-1.1.0",
    }
)
_MANIFEST_SCHEMA_VERSIONS_WITHOUT_FEED_BINDING = frozenset(
    {
        *_MANIFEST_SCHEMA_VERSIONS_WITHOUT_STRUCTURAL_WINDOW,
        ROOT_DATA_REQUEST_MANIFEST_SCHEMA_VERSION,
    }
)
LEGACY_NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION = "adaptive-native-ohlcv-bundle-1.0.0"
NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION = "adaptive-native-ohlcv-bundle-1.1.0"
BUNDLE_VALIDATION_SCHEMA_VERSION = "adaptive-native-bundle-validation-1.0.0"
MCP_AVAILABILITY_SCHEMA_VERSION = "adaptive-mcp-availability-1.0.0"
DATA_COMPARABILITY_POLICY_VERSION = "adaptive-data-comparability-1.1.0"

# This is the exact installed tool observed during the implementation audit.
# It returns TradingCursor analysis prose/chart metadata, not raw OHLCV rows.
TRADINGCURSOR_ANALYSIS_TOOL = "mcp__codex_apps__tradingcursor_request_analysis"


class MCPAvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_CAPABILITY = "insufficient_capability"


class BundleValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    NOT_COVERED = "not_covered"
    INCOMPARABLE = "incomparable"


class DataComparisonPurpose(StrEnum):
    STRUCTURAL_PRICE = "structural_price"
    SAME_WAVE_INDICATOR = "same_wave_indicator"
    SAME_TIMEFRAME_VOLUME = "same_timeframe_volume"
    CROSS_TIMEFRAME_INDICATOR = "cross_timeframe_indicator"
    CROSS_TIMEFRAME_VOLUME = "cross_timeframe_volume"


class DataComparabilityStatus(StrEnum):
    COMPARABLE = "comparable"
    INCOMPARABLE = "incomparable"


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


class _Contract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class TradingViewMCPAvailability(_Contract):
    status: MCPAvailabilityStatus
    inspected_tool_names: tuple[str, ...]
    raw_ohlcv_available: bool
    completed_candle_metadata_available: bool
    session_metadata_available: bool
    adjustment_metadata_available: bool
    pagination_metadata_available: bool
    exact_limitation: str
    inspected_at_utc: str
    schema_version: str = MCP_AVAILABILITY_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", MCPAvailabilityStatus(self.status))
        object.__setattr__(self, "inspected_tool_names", tuple(_text(item, field_name="inspected_tool_name") for item in self.inspected_tool_names))
        for name in (
            "raw_ohlcv_available",
            "completed_candle_metadata_available",
            "session_metadata_available",
            "adjustment_metadata_available",
            "pagination_metadata_available",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean.")
        object.__setattr__(self, "exact_limitation", _text(self.exact_limitation, field_name="exact_limitation"))
        object.__setattr__(self, "inspected_at_utc", _utc_text(self.inspected_at_utc, field_name="inspected_at_utc"))
        if self.content_hash and self.content_hash != tradingview_mcp_availability_content_hash(self):
            raise ValueError("TradingViewMCPAvailability content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "TradingViewMCPAvailability":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=tradingview_mcp_availability_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "TradingViewMCPAvailability":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != tradingview_mcp_availability_content_hash(result):
            raise ValueError("TradingViewMCPAvailability content_hash does not match its payload.")
        return result


def tradingview_mcp_availability_content_hash(
    value: TradingViewMCPAvailability | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, TradingViewMCPAvailability) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def current_codex_tradingview_mcp_availability(
    *, inspected_at_utc: str
) -> TradingViewMCPAvailability:
    """Return the audited capability boundary; this performs no tool call."""

    return TradingViewMCPAvailability.create(
        status=MCPAvailabilityStatus.INSUFFICIENT_CAPABILITY,
        inspected_tool_names=(TRADINGCURSOR_ANALYSIS_TOOL,),
        raw_ohlcv_available=False,
        completed_candle_metadata_available=False,
        session_metadata_available=False,
        adjustment_metadata_available=False,
        pagination_metadata_available=False,
        exact_limitation=(
            "The installed TradingCursor tool returns analysis text, chart ID, and status. "
            "It does not expose raw OHLCV, candle-completion, session, adjustment, or pagination metadata."
        ),
        inspected_at_utc=inspected_at_utc,
    )


@dataclass(frozen=True, slots=True)
class ParentChildDataLineage(_Contract):
    child_manifest_id: str
    child_manifest_hash: str
    parent_bundle_id: str
    parent_bundle_hash: str
    parent_candidate_id: str
    proof_feasibility_id: str
    proof_feasibility_hash: str
    parent_feed_family_hash: str
    parent_stream_hash: str
    expected_child_stream_hash: str
    schema_version: str = "adaptive-parent-child-data-lineage-1.0.0"
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "child_manifest_id",
            "parent_bundle_id",
            "parent_candidate_id",
            "proof_feasibility_id",
            "schema_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in (
            "child_manifest_hash",
            "parent_bundle_hash",
            "proof_feasibility_hash",
            "parent_feed_family_hash",
            "parent_stream_hash",
            "expected_child_stream_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        if self.content_hash and self.content_hash != parent_child_data_lineage_content_hash(self):
            raise ValueError("ParentChildDataLineage content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ParentChildDataLineage":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=parent_child_data_lineage_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ParentChildDataLineage":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != parent_child_data_lineage_content_hash(result):
            raise ValueError("ParentChildDataLineage content_hash does not match its payload.")
        return result


def parent_child_data_lineage_content_hash(
    value: ParentChildDataLineage | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ParentChildDataLineage) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class DataRequestManifest(_Contract):
    request_id: str
    planner_request_id: str
    planner_request_hash: str
    planner_decision_hash: str
    symbol: str
    provider: str
    source_interface: str
    provider_capability_set_hash: str
    canonical_timeframe: str
    provider_interval_alias: str
    parent_candidate_id: str
    parent_degree: str | None
    expected_child_degree: str | None
    structural_question: StructuralQuestion
    requested_start_utc: str
    requested_end_utc: str
    analysis_cutoff_utc: str
    session_policy: str
    adjustment_policy: str
    require_native: bool
    completed_candles_only: bool
    required_metadata_fields: tuple[str, ...]
    created_at_utc: str
    schema_version: str = DATA_REQUEST_MANIFEST_SCHEMA_VERSION
    content_hash: str = ""
    structural_scoring_start_utc: str | None = None
    structural_scoring_end_utc: str | None = None
    required_feed_identity: str | None = None
    parent_bundle_id: str | None = None
    parent_bundle_hash: str | None = None
    proof_feasibility_id: str | None = None
    proof_feasibility_hash: str | None = None
    parent_feed_family: MarketDataFeedFamily | None = None
    parent_stream_identity: MarketDataStreamIdentity | None = None
    expected_child_feed_family: MarketDataFeedFamily | None = None
    expected_child_stream_identity: MarketDataStreamIdentity | None = None

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "planner_request_id",
            "symbol",
            "provider",
            "source_interface",
            "provider_interval_alias",
            "parent_candidate_id",
            "session_policy",
            "adjustment_policy",
            "schema_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        for name in ("planner_request_hash", "planner_decision_hash", "provider_capability_set_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "structural_question", StructuralQuestion(self.structural_question))
        root_discovery = (
            self.structural_question is StructuralQuestion.DISCOVER_ROOT_STRUCTURE
        )
        if root_discovery:
            if self.parent_degree is not None or self.expected_child_degree is not None:
                raise ValueError(
                    "Root-discovery manifests cannot assign Elliott parent or child degrees."
                )
        else:
            object.__setattr__(
                self,
                "parent_degree",
                _text(self.parent_degree, field_name="parent_degree"),
            )
            object.__setattr__(
                self,
                "expected_child_degree",
                _text(
                    self.expected_child_degree,
                    field_name="expected_child_degree",
                ),
            )
        lineage_fields = (
            "parent_bundle_id",
            "parent_bundle_hash",
            "proof_feasibility_id",
            "proof_feasibility_hash",
            "parent_feed_family",
            "parent_stream_identity",
            "expected_child_feed_family",
            "expected_child_stream_identity",
        )
        if root_discovery:
            if any(getattr(self, name) is not None for name in lineage_fields):
                raise ValueError("Root-discovery manifests cannot carry parent-child lineage.")
        elif self.schema_version == DATA_REQUEST_MANIFEST_SCHEMA_VERSION:
            if self.required_feed_identity is not None:
                raise ValueError(
                    "Current child manifests use typed feed-family and stream identities, not required_feed_identity."
                )
            missing = [name for name in lineage_fields if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    "Current child manifests require complete parent-child lineage: "
                    + ", ".join(missing)
                    + "."
                )
            object.__setattr__(
                self,
                "parent_bundle_id",
                _text(self.parent_bundle_id, field_name="parent_bundle_id"),
            )
            object.__setattr__(
                self,
                "parent_bundle_hash",
                _hash(self.parent_bundle_hash, field_name="parent_bundle_hash"),
            )
            object.__setattr__(
                self,
                "proof_feasibility_id",
                _text(self.proof_feasibility_id, field_name="proof_feasibility_id"),
            )
            object.__setattr__(
                self,
                "proof_feasibility_hash",
                _hash(self.proof_feasibility_hash, field_name="proof_feasibility_hash"),
            )
            family = (
                self.parent_feed_family
                if isinstance(self.parent_feed_family, MarketDataFeedFamily)
                else MarketDataFeedFamily.from_dict(self.parent_feed_family)
            )
            parent_stream = (
                self.parent_stream_identity
                if isinstance(self.parent_stream_identity, MarketDataStreamIdentity)
                else MarketDataStreamIdentity.from_dict(self.parent_stream_identity)
            )
            expected_family = (
                self.expected_child_feed_family
                if isinstance(self.expected_child_feed_family, MarketDataFeedFamily)
                else MarketDataFeedFamily.from_dict(self.expected_child_feed_family)
            )
            expected_stream = (
                self.expected_child_stream_identity
                if isinstance(self.expected_child_stream_identity, MarketDataStreamIdentity)
                else MarketDataStreamIdentity.from_dict(self.expected_child_stream_identity)
            )
            if family.feed_family_hash != expected_family.feed_family_hash:
                raise ValueError("Parent and expected child feed families must match.")
            if (
                family.provider != self.provider
                or family.source_interface != self.source_interface
                or family.canonical_symbol != self.symbol
                or family.session_policy != self.session_policy
                or family.price_adjustment != self.adjustment_policy
            ):
                raise ValueError(
                    "Manifest feed family does not match its provider, source, symbol, session, or adjustment requirements."
                )
            if parent_stream.feed_family_hash != family.feed_family_hash:
                raise ValueError("Parent stream does not belong to the parent feed family.")
            if expected_stream.feed_family_hash != expected_family.feed_family_hash:
                raise ValueError("Expected child stream does not belong to the child feed family.")
            if parent_stream.stream_hash == expected_stream.stream_hash:
                raise ValueError("Cross-timeframe parent and child streams must be distinct.")
            if expected_stream.canonical_timeframe != normalize_timeframe_name(self.canonical_timeframe):
                raise ValueError("Expected child stream timeframe does not match the manifest.")
            if expected_stream.provider_interval_alias != self.provider_interval_alias:
                raise ValueError("Expected child provider interval does not match the manifest.")
            object.__setattr__(self, "parent_feed_family", family)
            object.__setattr__(self, "parent_stream_identity", parent_stream)
            object.__setattr__(self, "expected_child_feed_family", expected_family)
            object.__setattr__(self, "expected_child_stream_identity", expected_stream)
        object.__setattr__(self, "canonical_timeframe", normalize_timeframe_name(self.canonical_timeframe))
        for name in ("requested_start_utc", "requested_end_utc", "analysis_cutoff_utc", "created_at_utc"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        structural_start = self.structural_scoring_start_utc or self.requested_start_utc
        structural_end = self.structural_scoring_end_utc or self.requested_end_utc
        object.__setattr__(
            self,
            "structural_scoring_start_utc",
            _utc_text(
                structural_start,
                field_name="structural_scoring_start_utc",
            ),
        )
        object.__setattr__(
            self,
            "structural_scoring_end_utc",
            _utc_text(
                structural_end,
                field_name="structural_scoring_end_utc",
            ),
        )
        if _utc(self.requested_end_utc) <= _utc(self.requested_start_utc):
            raise ValueError("requested_end_utc must be after requested_start_utc.")
        if _utc(self.requested_end_utc) > _utc(self.analysis_cutoff_utc):
            raise ValueError("A data request cannot extend past the analysis cutoff.")
        if _utc(self.structural_scoring_end_utc) <= _utc(
            self.structural_scoring_start_utc
        ):
            raise ValueError(
                "structural_scoring_end_utc must follow structural_scoring_start_utc."
            )
        if _utc(self.requested_start_utc) > _utc(
            self.structural_scoring_start_utc
        ) or _utc(self.requested_end_utc) < _utc(self.structural_scoring_end_utc):
            raise ValueError(
                "Requested data must cover the complete structural scoring interval."
            )
        if _utc(self.structural_scoring_end_utc) > _utc(self.analysis_cutoff_utc):
            raise ValueError(
                "The structural scoring interval cannot extend past the analysis cutoff."
            )
        if not self.require_native or not self.completed_candles_only:
            raise ValueError("Adaptive proof requests require native completed candles.")
        if self.required_feed_identity is not None:
            object.__setattr__(
                self,
                "required_feed_identity",
                _text(self.required_feed_identity, field_name="required_feed_identity"),
            )
        required = tuple(sorted({_text(item, field_name="required_metadata_field") for item in self.required_metadata_fields}))
        if not required:
            raise ValueError("required_metadata_fields cannot be empty.")
        object.__setattr__(self, "required_metadata_fields", required)
        if self.content_hash and self.content_hash != data_request_manifest_content_hash(self):
            raise ValueError("DataRequestManifest content_hash does not match its payload.")

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        lineage_fields = (
            "parent_bundle_id",
            "parent_bundle_hash",
            "proof_feasibility_id",
            "proof_feasibility_hash",
            "parent_feed_family",
            "parent_stream_identity",
            "expected_child_feed_family",
            "expected_child_stream_identity",
        )
        if self.schema_version != DATA_REQUEST_MANIFEST_SCHEMA_VERSION:
            for name in lineage_fields:
                payload.pop(name, None)
        if self.schema_version in _MANIFEST_SCHEMA_VERSIONS_WITHOUT_STRUCTURAL_WINDOW:
            payload.pop("structural_scoring_start_utc", None)
            payload.pop("structural_scoring_end_utc", None)
            payload.pop("required_feed_identity", None)
        elif self.schema_version in _MANIFEST_SCHEMA_VERSIONS_WITHOUT_FEED_BINDING:
            payload.pop("required_feed_identity", None)
        elif self.required_feed_identity is None:
            payload.pop("required_feed_identity", None)
        return payload

    @classmethod
    def create(cls, **values: Any) -> "DataRequestManifest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=data_request_manifest_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "DataRequestManifest":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != data_request_manifest_content_hash(result):
            raise ValueError("DataRequestManifest content_hash does not match its payload.")
        return result


def data_request_manifest_content_hash(
    value: DataRequestManifest | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, DataRequestManifest) else dict(value)
    payload.pop("content_hash", None)
    if payload.get("schema_version") != DATA_REQUEST_MANIFEST_SCHEMA_VERSION:
        for name in (
            "parent_bundle_id",
            "parent_bundle_hash",
            "proof_feasibility_id",
            "proof_feasibility_hash",
            "parent_feed_family",
            "parent_stream_identity",
            "expected_child_feed_family",
            "expected_child_stream_identity",
        ):
            payload.pop(name, None)
    if payload.get("schema_version") in _MANIFEST_SCHEMA_VERSIONS_WITHOUT_STRUCTURAL_WINDOW:
        payload.pop("structural_scoring_start_utc", None)
        payload.pop("structural_scoring_end_utc", None)
        payload.pop("required_feed_identity", None)
    elif payload.get("schema_version") in _MANIFEST_SCHEMA_VERSIONS_WITHOUT_FEED_BINDING:
        payload.pop("required_feed_identity", None)
    return canonical_sha256(payload)


def _coverage_feed_family(
    request: AdaptiveTimeframeRequest,
    coverage: Any,
) -> MarketDataFeedFamily | None:
    required = (
        "provider",
        "source_interface",
        "provider_symbol",
        "exchange",
        "session_policy",
        "timezone",
        "adjustment_policy",
        "dividend_adjustment",
        "volume_adjustment",
        "price_basis",
    )
    if any(getattr(coverage, name, None) is None for name in required):
        return None
    family = build_feed_family(
        provider=coverage.provider,
        source_interface=coverage.source_interface,
        canonical_symbol=request.symbol,
        provider_symbol=coverage.provider_symbol,
        exchange=coverage.exchange,
        mic=coverage.mic,
        session_policy=coverage.session_policy,
        timezone=coverage.timezone,
        price_adjustment=coverage.adjustment_policy,
        dividend_adjustment=coverage.dividend_adjustment,
        volume_adjustment=coverage.volume_adjustment,
        price_basis=coverage.price_basis,
    )
    if coverage.feed_family_hash is not None and coverage.feed_family_hash != family.feed_family_hash:
        raise ValueError("Parent evidence feed-family hash does not match its typed fields.")
    return family


def _coverage_stream_identity(
    coverage: Any,
    family: MarketDataFeedFamily,
) -> MarketDataStreamIdentity | None:
    required = (
        "provider_interval_alias",
        "timestamp_semantics",
        "provider_bar_alignment",
    )
    if any(getattr(coverage, name, None) is None for name in required):
        return None
    stream = build_stream_identity(
        feed_family_hash=family.feed_family_hash,
        canonical_timeframe=coverage.timeframe,
        provider_interval_alias=coverage.provider_interval_alias,
        timestamp_semantics=coverage.timestamp_semantics,
        provider_bar_alignment=coverage.provider_bar_alignment,
        native=coverage.native,
        derived=not coverage.native,
    )
    if coverage.stream_hash is not None and coverage.stream_hash != stream.stream_hash:
        raise ValueError("Parent evidence stream hash does not match its typed fields.")
    return stream


def manifest_from_planner(
    request: AdaptiveTimeframeRequest,
    decision: AdaptiveTimeframeDecision,
    capabilities: ProviderCapabilitySet,
    *,
    source_interface: str,
) -> DataRequestManifest:
    if decision.request_content_hash != request.content_hash:
        raise ValueError("Planner decision does not belong to the request.")
    if decision.status is not TimeframePlanningStatus.PLANNED or decision.selected_timeframe is None:
        raise ValueError("Only a planned timeframe decision can become a data manifest.")
    if capabilities.content_hash != request.provider_capability_set_hash:
        raise ValueError("Provider capability hash mismatch.")
    capability = capabilities.available(decision.selected_timeframe)
    if capability is None or capability.provider_alias != decision.provider_alias:
        raise ValueError("Selected interval is not a matching native provider capability.")
    parent_coverage = next(
        (
            item
            for item in request.current_evidence_coverage
            if request.parent_timeframe is not None
            and item.timeframe == request.parent_timeframe
            and item.full_parent_coverage
            and item.native
        ),
        None,
    )
    parent_family = (
        _coverage_feed_family(request, parent_coverage)
        if parent_coverage is not None
        else None
    )
    parent_stream = (
        _coverage_stream_identity(parent_coverage, parent_family)
        if parent_coverage is not None and parent_family is not None
        else None
    )
    feasibility = decision.proof_feasibility
    complete_lineage = (
        parent_coverage is not None
        and parent_coverage.source_hash is not None
        and parent_family is not None
        and parent_stream is not None
        and feasibility is not None
    )
    expected_child_stream = (
        build_stream_identity(
            feed_family_hash=parent_family.feed_family_hash,
            canonical_timeframe=capability.canonical_name,
            provider_interval_alias=capability.provider_alias,
            timestamp_semantics=parent_stream.timestamp_semantics,
            provider_bar_alignment=capability.provider_bar_alignment,
            native=True,
            derived=False,
        )
        if complete_lineage
        else None
    )
    return DataRequestManifest.create(
        request_id=request.request_id,
        planner_request_id=request.request_id,
        planner_request_hash=request.content_hash,
        planner_decision_hash=decision.content_hash,
        symbol=request.symbol,
        provider=capabilities.provider,
        source_interface=source_interface,
        provider_capability_set_hash=capabilities.content_hash,
        canonical_timeframe=capability.canonical_name,
        provider_interval_alias=capability.provider_alias,
        parent_candidate_id=request.parent_candidate_id,
        parent_degree=request.parent_degree,
        expected_child_degree=request.expected_child_degree,
        structural_question=request.structural_question,
        requested_start_utc=request.requested_data_start_utc,
        requested_end_utc=request.requested_data_end_utc,
        analysis_cutoff_utc=request.cutoff_utc,
        session_policy=capability.session_policy,
        adjustment_policy=capability.adjustment_policy,
        require_native=True,
        completed_candles_only=True,
        required_metadata_fields=(
            "canonical_symbol",
            "provider_symbol",
            "exchange",
            "provider",
            "feed_identity",
            "session",
            "timezone",
            "price_adjustment",
            "dividend_adjustment",
            "price_basis",
            "timestamp_semantics",
            "completion_verification_method",
        )
        + (
            ("volume_adjustment",)
            if request.structural_question
            is not StructuralQuestion.DISCOVER_ROOT_STRUCTURE
            else ()
        ),
        created_at_utc=request.created_at_utc,
        structural_scoring_start_utc=request.parent_start_utc,
        structural_scoring_end_utc=request.parent_end_utc,
        required_feed_identity=(
            parent_coverage.feed_identity
            if parent_coverage is not None and not complete_lineage
            else None
        ),
        parent_bundle_id=(parent_coverage.source_reference if complete_lineage else None),
        parent_bundle_hash=(parent_coverage.source_hash if complete_lineage else None),
        proof_feasibility_id=(feasibility.feasibility_id if complete_lineage else None),
        proof_feasibility_hash=(feasibility.content_hash if complete_lineage else None),
        parent_feed_family=(parent_family if complete_lineage else None),
        parent_stream_identity=(parent_stream if complete_lineage else None),
        expected_child_feed_family=(parent_family if complete_lineage else None),
        expected_child_stream_identity=expected_child_stream,
        schema_version=(
            ROOT_DATA_REQUEST_MANIFEST_SCHEMA_VERSION
            if request.structural_question
            is StructuralQuestion.DISCOVER_ROOT_STRUCTURE
            else (
                DATA_REQUEST_MANIFEST_SCHEMA_VERSION
                if complete_lineage
                else LEGACY_BOUND_DATA_REQUEST_MANIFEST_SCHEMA_VERSION
            )
        ),
    )


def parent_child_lineage_from_manifest(
    manifest: DataRequestManifest,
) -> ParentChildDataLineage:
    if manifest.schema_version != DATA_REQUEST_MANIFEST_SCHEMA_VERSION:
        raise ValueError("Only current lineage-aware child manifests can create proof lineage.")
    if (
        manifest.parent_bundle_id is None
        or manifest.parent_bundle_hash is None
        or manifest.proof_feasibility_id is None
        or manifest.proof_feasibility_hash is None
        or manifest.parent_feed_family is None
        or manifest.parent_stream_identity is None
        or manifest.expected_child_stream_identity is None
    ):
        raise ValueError("Child manifest lineage is incomplete.")
    return ParentChildDataLineage.create(
        child_manifest_id=manifest.request_id,
        child_manifest_hash=manifest.content_hash,
        parent_bundle_id=manifest.parent_bundle_id,
        parent_bundle_hash=manifest.parent_bundle_hash,
        parent_candidate_id=manifest.parent_candidate_id,
        proof_feasibility_id=manifest.proof_feasibility_id,
        proof_feasibility_hash=manifest.proof_feasibility_hash,
        parent_feed_family_hash=manifest.parent_feed_family.feed_family_hash,
        parent_stream_hash=manifest.parent_stream_identity.stream_hash,
        expected_child_stream_hash=manifest.expected_child_stream_identity.stream_hash,
    )


@dataclass(frozen=True, slots=True)
class NativeOHLCVBundle(_Contract):
    bundle_id: str
    source_request_id: str
    source_request_hash: str
    provider_capability_set_hash: str
    canonical_symbol: str
    provider_symbol: str
    exchange: str
    provider: str
    source_interface: str
    feed_identity: str
    session: str
    timezone: str
    price_adjustment: str
    dividend_adjustment: str
    volume_adjustment: str
    price_basis: str
    timeframe: str
    provider_interval_alias: str
    native: bool
    derived: bool
    requested_start_utc: str
    requested_end_utc: str
    actual_first_candle_utc: str
    actual_final_completed_candle_utc: str
    acquisition_timestamp_utc: str
    analysis_cutoff_utc: str
    timestamp_semantics: str
    completion_verification_method: str
    incomplete_candles_excluded: bool
    source_response_hash: str
    candles: tuple[NativeCandle, ...]
    schema_version: str = LEGACY_NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION
    content_hash: str = ""
    file_hash: str = ""
    mic: str | None = None
    provider_bar_alignment: str | None = None
    feed_family: MarketDataFeedFamily | None = None
    stream_identity: MarketDataStreamIdentity | None = None
    proof_lineage: ParentChildDataLineage | None = None

    def __post_init__(self) -> None:
        for name in (
            "bundle_id",
            "source_request_id",
            "canonical_symbol",
            "provider_symbol",
            "exchange",
            "provider",
            "source_interface",
            "feed_identity",
            "session",
            "timezone",
            "price_adjustment",
            "dividend_adjustment",
            "volume_adjustment",
            "price_basis",
            "provider_interval_alias",
            "timestamp_semantics",
            "completion_verification_method",
            "schema_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if self.mic is not None:
            object.__setattr__(self, "mic", _text(self.mic, field_name="mic"))
        object.__setattr__(
            self,
            "provider_bar_alignment",
            canonical_bar_alignment(
                self.provider_bar_alignment,
                timestamp_semantics=self.timestamp_semantics,
            ).value,
        )
        for name in ("source_request_hash", "provider_capability_set_hash", "source_response_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), field_name=name))
        object.__setattr__(self, "timeframe", normalize_timeframe_name(self.timeframe))
        if not isinstance(self.native, bool) or not isinstance(self.derived, bool):
            raise TypeError("native and derived must be boolean.")
        if not self.native or self.derived:
            raise ValueError("Adaptive orthodox-pivot bundles must be provider-native, not derived.")
        for name in (
            "requested_start_utc",
            "requested_end_utc",
            "actual_first_candle_utc",
            "actual_final_completed_candle_utc",
            "acquisition_timestamp_utc",
            "analysis_cutoff_utc",
        ):
            object.__setattr__(self, name, _utc_text(getattr(self, name), field_name=name))
        candles = tuple(
            item if isinstance(item, NativeCandle) else NativeCandle.from_dict(item)
            for item in self.candles
        )
        if not candles:
            raise ValueError("NativeOHLCVBundle requires candles.")
        timestamps = tuple(_utc(item.timestamp_utc) for item in candles)
        if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
            raise ValueError("Bundle candle timestamps must be strictly increasing and unique.")
        if any(item.source_row_hash != native_candle_content_hash(item) for item in candles):
            raise ValueError("Bundle contains a candle with an invalid source-row hash.")
        if any(not item.completed for item in candles) or not self.incomplete_candles_excluded:
            raise ValueError("Bundle may contain completed candles only.")
        if timestamps[-1] > _utc(self.analysis_cutoff_utc):
            raise ValueError("Bundle contains a candle after the analysis cutoff.")
        if _utc(self.actual_first_candle_utc) != timestamps[0] or _utc(self.actual_final_completed_candle_utc) != timestamps[-1]:
            raise ValueError("Actual first/final timestamps must match the candle rows.")
        object.__setattr__(self, "candles", candles)
        derived_family = build_feed_family(
            provider=self.provider,
            source_interface=self.source_interface,
            canonical_symbol=self.canonical_symbol,
            provider_symbol=self.provider_symbol,
            exchange=self.exchange,
            mic=self.mic,
            session_policy=self.session,
            timezone=self.timezone,
            price_adjustment=self.price_adjustment,
            dividend_adjustment=self.dividend_adjustment,
            volume_adjustment=self.volume_adjustment,
            price_basis=self.price_basis,
        )
        family = (
            derived_family
            if self.feed_family is None
            else (
                self.feed_family
                if isinstance(self.feed_family, MarketDataFeedFamily)
                else MarketDataFeedFamily.from_dict(self.feed_family)
            )
        )
        if family.feed_family_hash != derived_family.feed_family_hash:
            raise ValueError("NativeOHLCVBundle feed family does not match its typed metadata.")
        derived_stream = build_stream_identity(
            feed_family_hash=family.feed_family_hash,
            canonical_timeframe=self.timeframe,
            provider_interval_alias=self.provider_interval_alias,
            timestamp_semantics=self.timestamp_semantics,
            provider_bar_alignment=self.provider_bar_alignment,
            native=self.native,
            derived=self.derived,
        )
        stream = (
            derived_stream
            if self.stream_identity is None
            else (
                self.stream_identity
                if isinstance(self.stream_identity, MarketDataStreamIdentity)
                else MarketDataStreamIdentity.from_dict(self.stream_identity)
            )
        )
        if stream.stream_hash != derived_stream.stream_hash:
            raise ValueError("NativeOHLCVBundle stream identity does not match its typed metadata.")
        lineage = self.proof_lineage
        if lineage is not None and not isinstance(lineage, ParentChildDataLineage):
            lineage = ParentChildDataLineage.from_dict(lineage)
        object.__setattr__(self, "feed_family", family)
        object.__setattr__(self, "stream_identity", stream)
        object.__setattr__(self, "proof_lineage", lineage)
        if self.content_hash and self.content_hash != native_ohlcv_bundle_content_hash(self):
            raise ValueError("NativeOHLCVBundle content_hash does not match its payload.")
        if self.file_hash and self.file_hash != native_ohlcv_bundle_file_hash(self):
            raise ValueError("NativeOHLCVBundle file_hash does not match its canonical file payload.")

    @classmethod
    def create(cls, **values: Any) -> "NativeOHLCVBundle":
        if values.pop("content_hash", "") or values.pop("file_hash", ""):
            raise ValueError("create() calculates hashes; do not supply them.")
        seed = cls(**values)
        content = native_ohlcv_bundle_content_hash(seed)
        with_content = replace(seed, content_hash=content)
        return replace(with_content, file_hash=native_ohlcv_bundle_file_hash(with_content))

    @classmethod
    def create_for_manifest(
        cls,
        manifest: DataRequestManifest,
        **values: Any,
    ) -> "NativeOHLCVBundle":
        """Create a bundle with the manifest's immutable proof lineage.

        The caller must still supply truthful provider metadata and candle rows;
        the normal contract validation rejects any disagreement with these
        expected identities.
        """

        if manifest.schema_version == DATA_REQUEST_MANIFEST_SCHEMA_VERSION:
            values.setdefault("proof_lineage", parent_child_lineage_from_manifest(manifest))
        values.setdefault("schema_version", NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION)
        return cls.create(**values)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "NativeOHLCVBundle":
        result = cls(**dict(value))
        if verify_hash and (
            result.content_hash != native_ohlcv_bundle_content_hash(result)
            or result.file_hash != native_ohlcv_bundle_file_hash(result)
        ):
            raise ValueError("NativeOHLCVBundle hashes do not match its payload.")
        return result

    def to_dict(self) -> dict[str, Any]:
        payload = _Contract.to_dict(self)
        if self.schema_version == LEGACY_NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION:
            for name in (
                "mic",
                "provider_bar_alignment",
                "feed_family",
                "stream_identity",
                "proof_lineage",
            ):
                payload.pop(name, None)
        else:
            if self.mic is None:
                payload.pop("mic", None)
            if self.proof_lineage is None:
                payload.pop("proof_lineage", None)
        return payload


def native_ohlcv_bundle_content_hash(
    value: NativeOHLCVBundle | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, NativeOHLCVBundle) else dict(value)
    payload.pop("content_hash", None)
    payload.pop("file_hash", None)
    return canonical_sha256(payload)


def native_ohlcv_bundle_file_hash(
    value: NativeOHLCVBundle | Mapping[str, Any],
) -> str:
    """Hash the canonical file payload with only the self-referential field blank."""

    payload = value.to_dict() if isinstance(value, NativeOHLCVBundle) else dict(value)
    payload.pop("file_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class BundleValidationResult(_Contract):
    status: BundleValidationStatus
    request_id: str
    bundle_id: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    comparable_for_price_structure: bool
    native_complete_coverage: bool
    schema_version: str = BUNDLE_VALIDATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", BundleValidationStatus(self.status))
        object.__setattr__(self, "request_id", _text(self.request_id, field_name="request_id"))
        if self.bundle_id is not None:
            object.__setattr__(self, "bundle_id", _text(self.bundle_id, field_name="bundle_id"))
        object.__setattr__(self, "errors", tuple(str(item) for item in self.errors))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        if not isinstance(self.comparable_for_price_structure, bool):
            raise TypeError("comparable_for_price_structure must be boolean.")
        if not isinstance(self.native_complete_coverage, bool):
            raise TypeError("native_complete_coverage must be boolean.")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BundleValidationResult":
        return cls(**dict(value))


def _final_candle_covers_requested_end(
    bundle: NativeOHLCVBundle,
    requested_end_utc: str,
) -> bool:
    """Interpret an explicitly declared bar-open label as an interval.

    Daily, weekly, and intraday providers commonly timestamp a completed candle
    at its opening boundary.  Comparing that label directly with a session-close
    cutoff incorrectly reports missing coverage.  This helper is deliberately
    unavailable for unknown timestamp semantics and still requires acquisition
    after the requested endpoint.
    """

    final_label = _utc(bundle.actual_final_completed_candle_utc)
    requested_end = _utc(requested_end_utc)
    if final_label >= requested_end:
        return True
    semantics = bundle.timestamp_semantics.casefold()
    if "bar_open" not in semantics and "period_start" not in semantics:
        return False
    if _utc(bundle.acquisition_timestamp_utc) < requested_end:
        return False

    timeframe = normalize_timeframe_name(bundle.timeframe)
    if timeframe == "monthly":
        if final_label.month == 12:
            interval_end = final_label.replace(
                year=final_label.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            interval_end = final_label.replace(
                month=final_label.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        return requested_end < interval_end
    interval_end = final_label + timedelta(
        seconds=timeframe_duration_seconds(timeframe)
    )
    return requested_end <= interval_end


def validate_bundle_for_manifest(
    manifest: DataRequestManifest,
    bundle: NativeOHLCVBundle,
    capabilities: ProviderCapabilitySet,
) -> BundleValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if capabilities.content_hash != provider_capability_set_content_hash(capabilities):
        errors.append("provider_capability_hash_invalid")
    if manifest.provider_capability_set_hash != capabilities.content_hash or bundle.provider_capability_set_hash != capabilities.content_hash:
        errors.append("provider_capability_lineage_mismatch")
    if bundle.source_request_id != manifest.request_id or bundle.source_request_hash != manifest.content_hash:
        errors.append("source_request_lineage_mismatch")
    comparisons = {
        "canonical_symbol": (bundle.canonical_symbol, manifest.symbol),
        "provider": (bundle.provider, manifest.provider),
        "source_interface": (bundle.source_interface, manifest.source_interface),
        "timeframe": (bundle.timeframe, manifest.canonical_timeframe),
        "provider_interval_alias": (bundle.provider_interval_alias, manifest.provider_interval_alias),
        "session": (bundle.session, manifest.session_policy),
        "price_adjustment": (bundle.price_adjustment, manifest.adjustment_policy),
        "analysis_cutoff": (bundle.analysis_cutoff_utc, manifest.analysis_cutoff_utc),
    }
    for field_name, (actual, expected) in comparisons.items():
        if actual != expected:
            errors.append(f"{field_name}_mismatch")
    if (
        bundle.timeframe != manifest.canonical_timeframe
        or bundle.provider_interval_alias != manifest.provider_interval_alias
    ):
        errors.append("expected_child_interval_mismatch")
    if manifest.schema_version == DATA_REQUEST_MANIFEST_SCHEMA_VERSION:
        expected_family = manifest.expected_child_feed_family
        expected_stream = manifest.expected_child_stream_identity
        if expected_family is None or expected_stream is None:
            errors.append("parent_child_feed_lineage_mismatch")
        else:
            family_fields = {
                "provider_product": (
                    bundle.feed_family.source_interface,
                    expected_family.source_interface,
                ),
                "provider_symbol": (
                    bundle.feed_family.provider_symbol,
                    expected_family.provider_symbol,
                ),
                "exchange": (bundle.feed_family.exchange, expected_family.exchange),
                "mic": (bundle.feed_family.mic, expected_family.mic),
                "session": (
                    bundle.feed_family.session_policy,
                    expected_family.session_policy,
                ),
                "timezone": (bundle.feed_family.timezone, expected_family.timezone),
                "adjustment": (
                    (
                        bundle.feed_family.price_adjustment,
                        bundle.feed_family.dividend_adjustment,
                        bundle.feed_family.volume_adjustment,
                    ),
                    (
                        expected_family.price_adjustment,
                        expected_family.dividend_adjustment,
                        expected_family.volume_adjustment,
                    ),
                ),
                "price_basis": (
                    bundle.feed_family.price_basis,
                    expected_family.price_basis,
                ),
            }
            for field_name, (actual, expected) in family_fields.items():
                if actual != expected:
                    errors.append(f"{field_name}_mismatch")
            if bundle.feed_family.feed_family_hash != expected_family.feed_family_hash:
                errors.append("feed_family_mismatch")
            if bundle.stream_identity.stream_hash != expected_stream.stream_hash:
                errors.append("child_stream_identity_mismatch")
            expected_legacy = twelve_data_legacy_stream_identity(
                expected_family,
                canonical_timeframe=manifest.canonical_timeframe,
            )
            if expected_legacy is not None and bundle.feed_identity != expected_legacy:
                errors.append("child_stream_identity_mismatch")
        expected_lineage = parent_child_lineage_from_manifest(manifest)
        if (
            bundle.proof_lineage is None
            or bundle.proof_lineage.content_hash != expected_lineage.content_hash
        ):
            errors.append("parent_child_feed_lineage_mismatch")
    elif (
        manifest.required_feed_identity is not None
        and bundle.feed_identity != manifest.required_feed_identity
    ):
        errors.append("feed_identity_mismatch")
    capability = capabilities.available(manifest.canonical_timeframe)
    if capability is None or capability.provider_alias != manifest.provider_interval_alias:
        errors.append("provider_interval_not_advertised")
    full_coverage = (
        _utc(bundle.actual_first_candle_utc) <= _utc(manifest.requested_start_utc)
        and _final_candle_covers_requested_end(bundle, manifest.requested_end_utc)
    )
    if not full_coverage:
        warnings.append("native_parent_interval_not_fully_covered")
    if errors:
        status = BundleValidationStatus.INVALID
    elif not full_coverage:
        status = BundleValidationStatus.NOT_COVERED
    else:
        status = BundleValidationStatus.VALID
    return BundleValidationResult(
        status=status,
        request_id=manifest.request_id,
        bundle_id=bundle.bundle_id,
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(warnings),
        comparable_for_price_structure=not errors,
        native_complete_coverage=full_coverage and not errors,
    )


@dataclass(frozen=True, slots=True)
class DataComparabilityResult(_Contract):
    status: DataComparabilityStatus
    purpose: DataComparisonPurpose
    left_bundle_id: str
    right_bundle_id: str
    comparable_fields: tuple[str, ...]
    incomparable_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    policy_version: str = DATA_COMPARABILITY_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DataComparabilityStatus(self.status))
        object.__setattr__(self, "purpose", DataComparisonPurpose(self.purpose))


def compare_bundle_identity(
    left: NativeOHLCVBundle,
    right: NativeOHLCVBundle,
    *,
    purpose: DataComparisonPurpose,
) -> DataComparabilityResult:
    purpose = DataComparisonPurpose(purpose)
    values: dict[str, tuple[Any, Any]] = {
        "feed_family_hash": (
            left.feed_family.feed_family_hash,
            right.feed_family.feed_family_hash,
        ),
        "analysis_cutoff_utc": (left.analysis_cutoff_utc, right.analysis_cutoff_utc),
    }
    if purpose in {
        DataComparisonPurpose.SAME_WAVE_INDICATOR,
        DataComparisonPurpose.SAME_TIMEFRAME_VOLUME,
    }:
        values.update(
            {
                "canonical_timeframe": (left.timeframe, right.timeframe),
                "stream_hash": (
                    left.stream_identity.stream_hash,
                    right.stream_identity.stream_hash,
                ),
                "legacy_feed_identity": (left.feed_identity, right.feed_identity),
            }
        )
    comparable = tuple(name for name, pair in values.items() if pair[0] == pair[1])
    incomparable = tuple(name for name, pair in values.items() if pair[0] != pair[1])
    warnings: tuple[str, ...] = ()
    force_incomparable = purpose in {
        DataComparisonPurpose.CROSS_TIMEFRAME_INDICATOR,
        DataComparisonPurpose.CROSS_TIMEFRAME_VOLUME,
    }
    if force_incomparable:
        incomparable = tuple(dict.fromkeys((*incomparable, "cross_timeframe_methodology")))
        warnings = (
            "Cross-timeframe indicators and volume remain incomparable without an explicitly approved methodology.",
        )
    elif purpose is DataComparisonPurpose.SAME_TIMEFRAME_VOLUME and incomparable:
        warnings = ("Volume remains quarantined outside one unchanged feed and timeframe.",)
    elif purpose is DataComparisonPurpose.SAME_WAVE_INDICATOR and incomparable:
        warnings = ("Indicator comparison requires one unchanged feed-family and interval stream.",)
    return DataComparabilityResult(
        status=(
            DataComparabilityStatus.COMPARABLE
            if not incomparable and not force_incomparable
            else DataComparabilityStatus.INCOMPARABLE
        ),
        purpose=purpose,
        left_bundle_id=left.bundle_id,
        right_bundle_id=right.bundle_id,
        comparable_fields=comparable,
        incomparable_fields=incomparable,
        warnings=warnings,
    )


def write_immutable_bundle(bundle: NativeOHLCVBundle, output_directory: Path) -> Path:
    """Write one canonical content-addressed artifact without overwriting."""

    if bundle.content_hash != native_ohlcv_bundle_content_hash(bundle) or bundle.file_hash != native_ohlcv_bundle_file_hash(bundle):
        raise ValueError("Bundle hashes must validate before writing.")
    directory = Path(output_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{bundle.source_request_id}_{bundle.timeframe}_{bundle.content_hash[:20]}.json"
    if target.exists():
        raise FileExistsError(f"Immutable bundle already exists: {target}")
    data = (canonical_forecast_json(bundle.to_dict()) + "\n").encode("utf-8")
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists():
        raise FileExistsError(f"Temporary artifact already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise FileExistsError(f"Immutable bundle already exists: {target}")
        temporary.rename(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    loaded = NativeOHLCVBundle.from_dict(json.loads(target.read_text(encoding="utf-8")))
    if loaded.file_hash != bundle.file_hash:
        raise ValueError("Written bundle failed canonical file-hash validation.")
    return target


__all__ = [
    "BUNDLE_VALIDATION_SCHEMA_VERSION",
    "DATA_COMPARABILITY_POLICY_VERSION",
    "DATA_REQUEST_MANIFEST_SCHEMA_VERSION",
    "LEGACY_BOUND_DATA_REQUEST_MANIFEST_SCHEMA_VERSION",
    "LEGACY_NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION",
    "MCP_AVAILABILITY_SCHEMA_VERSION",
    "NATIVE_OHLCV_BUNDLE_SCHEMA_VERSION",
    "ROOT_DATA_REQUEST_MANIFEST_SCHEMA_VERSION",
    "TRADINGCURSOR_ANALYSIS_TOOL",
    "BundleValidationResult",
    "BundleValidationStatus",
    "DataComparabilityResult",
    "DataComparabilityStatus",
    "DataComparisonPurpose",
    "DataRequestManifest",
    "MCPAvailabilityStatus",
    "NativeOHLCVBundle",
    "ParentChildDataLineage",
    "TradingViewMCPAvailability",
    "compare_bundle_identity",
    "current_codex_tradingview_mcp_availability",
    "data_request_manifest_content_hash",
    "manifest_from_planner",
    "native_ohlcv_bundle_content_hash",
    "native_ohlcv_bundle_file_hash",
    "parent_child_data_lineage_content_hash",
    "parent_child_lineage_from_manifest",
    "tradingview_mcp_availability_content_hash",
    "validate_bundle_for_manifest",
    "write_immutable_bundle",
]
