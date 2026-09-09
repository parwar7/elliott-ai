"""Canonical timeframe and provider-capability contracts.

Timeframes describe evidence granularity, not Elliott degree.  The canonical
parser accepts the repository's legacy names and provider aliases while
preserving the important distinction between ``1M`` (one calendar month) and
``1m`` (one minute).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Any, Iterable, Mapping, Sequence

from .forecast_records import canonical_sha256


TIMEFRAME_SCHEMA_VERSION = "canonical-timeframe-1.0.0"
PROVIDER_TIMEFRAME_CAPABILITY_SCHEMA_VERSION = (
    "provider-timeframe-capability-1.1.0"
)
PROVIDER_CAPABILITY_SET_SCHEMA_VERSION = "provider-timeframe-capability-set-1.0.0"
DEGREE_TIMEFRAME_POLICY_VERSION = "adaptive-degree-timeframe-policy-1.0.0"
PURPOSE_AWARE_TIMEFRAME_POLICY_VERSION = (
    "purpose-aware-timeframe-use-policy-1.0.0"
)
_LEGACY_PROVIDER_TIMEFRAME_CAPABILITY_SCHEMA_VERSIONS = frozenset(
    {"provider-timeframe-capability-1.0.0"}
)


class TimeframeClass(StrEnum):
    INTRADAY = "intraday"
    HIGHER_TIMEFRAME = "higher_timeframe"


class TimeframeAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class MarketSessionMode(StrEnum):
    CONTINUOUS_24_7 = "continuous_24_7"
    REGULAR_SESSION_EQUITY = "regular_session_equity"
    EXTENDED_SESSION_EQUITY = "extended_session_equity"
    UNKNOWN = "unknown"


class TimeframeUsePurpose(StrEnum):
    ROOT_STRUCTURE_OBSERVATION = "root_structure_observation"
    SUBDIVISION_PROOF = "subdivision_proof"
    TERMINAL_REFINEMENT = "terminal_refinement"
    LEGACY_DEGREE_RESOLUTION = "legacy_degree_resolution"


class ProviderBarAlignment(StrEnum):
    ELAPSED_TIME = "elapsed_time"
    SESSION_DAILY = "session_daily"
    CALENDAR_BUCKET = "calendar_bucket"
    SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL = (
        "session_open_aligned_with_short_final"
    )
    SESSION_OPEN_ALIGNED_FULL_ONLY = "session_open_aligned_full_only"
    UNKNOWN = "unknown"


_NAMED_ALIASES = {
    "monthly": "monthly",
    "month": "monthly",
    "1month": "monthly",
    "1mo": "monthly",
    "weekly": "weekly",
    "week": "weekly",
    "1week": "weekly",
    "1w": "weekly",
    "daily": "daily",
    "day": "daily",
    "1day": "daily",
    "1d": "daily",
}
_SPECIAL_CASE_ALIASES = {
    "M": "monthly",
    "1M": "monthly",
    "W": "weekly",
    "1W": "weekly",
    "D": "daily",
    "1D": "daily",
}
_INTERVAL_PATTERN = re.compile(
    r"^(?P<count>[1-9]\d*)\s*(?P<unit>minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)$",
    re.IGNORECASE,
)


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _utc_text(value: Any, *, field_name: str) -> str:
    raw = _require_text(value, field_name=field_name)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone offset.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_time_text(value: Any, *, field_name: str) -> str:
    raw = _require_text(value, field_name=field_name)
    match = re.fullmatch(r"(?P<hour>\d{2}):(?P<minute>\d{2})", raw)
    if match is None:
        raise ValueError(f"{field_name} must use HH:MM.")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise ValueError(f"{field_name} must be a valid local time.")
    return f"{hour:02d}:{minute:02d}"


def _canonical_from_seconds(seconds: int) -> str:
    if seconds == 7 * 24 * 60 * 60:
        return "weekly"
    if seconds == 24 * 60 * 60:
        return "daily"
    if seconds % (24 * 60 * 60) == 0:
        return f"{seconds // (24 * 60 * 60)}d"
    if seconds % (60 * 60) == 0:
        return f"{seconds // (60 * 60)}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    raise ValueError("Timeframe duration must resolve to whole minutes.")


def normalize_timeframe_name(value: Any) -> str:
    """Return a stable canonical name for a legacy name or provider alias."""

    raw = _require_text(value, field_name="timeframe")
    if raw in _SPECIAL_CASE_ALIASES:
        return _SPECIAL_CASE_ALIASES[raw]
    lowered = raw.casefold().replace("_", "").replace("-", "")
    if lowered in _NAMED_ALIASES:
        return _NAMED_ALIASES[lowered]
    # Provider aliases commonly insert separators (for example ``2-day`` or
    # ``45_min``).  The exact-case month aliases above are resolved first so
    # compacting an interval cannot collapse ``1M`` into one minute.
    compact = raw.strip().replace(" ", "").replace("_", "").replace("-", "")
    match = _INTERVAL_PATTERN.fullmatch(compact)
    if match is None:
        raise ValueError(f"Unsupported timeframe syntax: {raw!r}.")
    count = int(match.group("count"))
    unit = match.group("unit").casefold()
    if unit.startswith(("minute", "min")) or unit == "m":
        seconds = count * 60
    elif unit.startswith(("hour", "hr")) or unit == "h":
        seconds = count * 60 * 60
    elif unit.startswith("day") or unit == "d":
        seconds = count * 24 * 60 * 60
    else:
        seconds = count * 7 * 24 * 60 * 60
    return _canonical_from_seconds(seconds)


def timeframe_duration_seconds(value: Any) -> int:
    canonical = normalize_timeframe_name(value)
    if canonical == "monthly":
        # Nominal duration is used for deterministic ordering and bar planning;
        # actual calendar-month boundaries remain provider/session metadata.
        return 30 * 24 * 60 * 60
    if canonical == "weekly":
        return 7 * 24 * 60 * 60
    if canonical == "daily":
        return 24 * 60 * 60
    match = re.fullmatch(r"([1-9]\d*)([dhm])", canonical)
    if match is None:
        raise ValueError(f"Unsupported canonical timeframe: {canonical!r}.")
    count = int(match.group(1))
    unit = match.group(2)
    return count * {"d": 86_400, "h": 3_600, "m": 60}[unit]


def timeframe_class(value: Any) -> TimeframeClass:
    return (
        TimeframeClass.INTRADAY
        if timeframe_duration_seconds(value) < 86_400
        else TimeframeClass.HIGHER_TIMEFRAME
    )


def timeframe_is_finer(child: Any, parent: Any) -> bool:
    return timeframe_duration_seconds(child) < timeframe_duration_seconds(parent)


def timeframe_is_same_or_finer(child: Any, parent: Any) -> bool:
    """Return whether child evidence is no coarser than its parent evidence.

    Existing shadow contracts permit a first subdivision pass on the same
    native timeframe.  The adaptive planner uses :func:`timeframe_is_finer`
    when it requests *additional* data, so that workflow still descends in
    granularity rather than issuing a redundant request.
    """

    return timeframe_duration_seconds(child) <= timeframe_duration_seconds(parent)


def timeframe_sort_key(value: Any, *, coarse_first: bool = True) -> tuple[int, str]:
    canonical = normalize_timeframe_name(value)
    duration = timeframe_duration_seconds(canonical)
    return ((-duration if coarse_first else duration), canonical)


def sort_timeframes(
    values: Iterable[Any], *, coarse_first: bool = True
) -> tuple[str, ...]:
    canonical = {normalize_timeframe_name(value) for value in values}
    return tuple(
        sorted(canonical, key=lambda item: timeframe_sort_key(item, coarse_first=coarse_first))
    )


# The bands preserve every legacy pairing while allowing provider-supported
# intermediate resolutions.  They are evidence-scale policy, not assertions
# about how long a degree must last.
_DEGREE_GRANULARITY_BANDS = {
    "Grand Supercycle": (7 * 86_400, 30 * 86_400),
    "Supercycle": (7 * 86_400, 30 * 86_400),
    "Cycle": (7 * 86_400, 30 * 86_400),
    "Primary": (4 * 3_600, 3 * 86_400),
    "Intermediate": (4 * 3_600, 3 * 86_400),
    "Minor": (4 * 3_600, 3 * 86_400),
    "Minute": (5 * 60, 3 * 3_600),
    "Minuette": (5 * 60, 3 * 3_600),
    "Subminuette": (5 * 60, 3 * 3_600),
}


@dataclass(frozen=True, slots=True)
class DegreeTimeframeCompatibility:
    degree: str
    timeframe: str
    compatible: bool
    reason: str
    policy_version: str = DEGREE_TIMEFRAME_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "degree": self.degree,
            "timeframe": self.timeframe,
            "compatible": self.compatible,
            "reason": self.reason,
            "policy_version": self.policy_version,
        }


def degree_timeframe_compatibility(
    degree: Any,
    timeframe: Any,
    *,
    allowed_timeframes: Mapping[str, Sequence[str]] | None = None,
) -> DegreeTimeframeCompatibility:
    degree_text = str(degree).strip()
    if degree_text == "Unassigned":
        return DegreeTimeframeCompatibility(
            degree=degree_text,
            timeframe=str(timeframe),
            compatible=str(timeframe).strip() == "unknown",
            reason="Unassigned metadata is not a normal wave degree.",
        )
    try:
        canonical = normalize_timeframe_name(timeframe)
    except ValueError as exc:
        return DegreeTimeframeCompatibility(
            degree=degree_text,
            timeframe=str(timeframe),
            compatible=False,
            reason=str(exc),
        )
    if allowed_timeframes is not None and degree_text in allowed_timeframes:
        allowed = {
            normalize_timeframe_name(item) for item in allowed_timeframes[degree_text]
        }
        return DegreeTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            compatible=canonical in allowed,
            reason=(
                "Permitted by the supplied versioned degree/timeframe policy."
                if canonical in allowed
                else "Outside the supplied versioned degree/timeframe policy."
            ),
        )
    band = _DEGREE_GRANULARITY_BANDS.get(degree_text)
    if band is None:
        return DegreeTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            compatible=False,
            reason="Degree is not part of the standard adaptive policy.",
        )
    duration = timeframe_duration_seconds(canonical)
    compatible = band[0] <= duration <= band[1]
    return DegreeTimeframeCompatibility(
        degree=degree_text,
        timeframe=canonical,
        compatible=compatible,
        reason=(
            "Granularity is inside the adaptive degree evidence band."
            if compatible
            else "Granularity is outside the adaptive degree evidence band."
        ),
    )


def timeframe_is_compatible_with_degree(degree: Any, timeframe: Any) -> bool:
    return degree_timeframe_compatibility(degree, timeframe).compatible


@dataclass(frozen=True, slots=True)
class PurposeAwareTimeframeCompatibility:
    degree: str
    timeframe: str
    purpose: TimeframeUsePurpose
    compatible: bool
    reason_codes: tuple[str, ...]
    reason: str
    parent_degree: str | None = None
    parent_timeframe: str | None = None
    policy_version: str = PURPOSE_AWARE_TIMEFRAME_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "degree": self.degree,
            "timeframe": self.timeframe,
            "purpose": self.purpose.value,
            "compatible": self.compatible,
            "reason_codes": list(self.reason_codes),
            "reason": self.reason,
            "parent_degree": self.parent_degree,
            "parent_timeframe": self.parent_timeframe,
            "policy_version": self.policy_version,
        }


def timeframe_compatibility_for_purpose(
    degree: Any,
    timeframe: Any,
    *,
    purpose: TimeframeUsePurpose | str,
    parent_degree: Any | None = None,
    parent_timeframe: Any | None = None,
) -> PurposeAwareTimeframeCompatibility:
    """Validate evidence resolution without treating it as Elliott degree.

    Root observation and the legacy resolver retain the existing absolute
    granularity bands.  Subdivision proof is relational: the structural child
    must descend exactly one Elliott degree while its evidence timeframe must
    be strictly finer than the parent's evidence timeframe.
    """

    purpose_value = TimeframeUsePurpose(purpose)
    degree_text = str(degree).strip()
    try:
        canonical = normalize_timeframe_name(timeframe)
    except ValueError as exc:
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=str(timeframe),
            purpose=purpose_value,
            compatible=False,
            reason_codes=("timeframe_invalid",),
            reason=str(exc),
            parent_degree=(None if parent_degree is None else str(parent_degree).strip()),
            parent_timeframe=(None if parent_timeframe is None else str(parent_timeframe)),
        )

    if purpose_value in {
        TimeframeUsePurpose.ROOT_STRUCTURE_OBSERVATION,
        TimeframeUsePurpose.LEGACY_DEGREE_RESOLUTION,
    }:
        legacy = degree_timeframe_compatibility(degree_text, canonical)
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=legacy.compatible,
            reason_codes=(() if legacy.compatible else ("outside_absolute_degree_band",)),
            reason=legacy.reason,
        )

    assigned_degrees = tuple(_DEGREE_GRANULARITY_BANDS)
    parent_degree_text = None if parent_degree is None else str(parent_degree).strip()
    if degree_text not in assigned_degrees:
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=False,
            reason_codes=("child_degree_invalid",),
            reason="Subdivision proof requires an assigned standard child degree.",
            parent_degree=parent_degree_text,
            parent_timeframe=(None if parent_timeframe is None else str(parent_timeframe)),
        )
    if parent_degree_text not in assigned_degrees:
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=False,
            reason_codes=("parent_degree_invalid",),
            reason="Subdivision proof requires an assigned standard parent degree.",
            parent_degree=parent_degree_text,
            parent_timeframe=(None if parent_timeframe is None else str(parent_timeframe)),
        )
    parent_index = assigned_degrees.index(parent_degree_text)
    expected_child = (
        assigned_degrees[parent_index + 1]
        if parent_index + 1 < len(assigned_degrees)
        else None
    )
    if degree_text != expected_child:
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=False,
            reason_codes=("direct_child_degree_descent_invalid",),
            reason="Subdivision proof must descend exactly one Elliott degree.",
            parent_degree=parent_degree_text,
            parent_timeframe=(None if parent_timeframe is None else str(parent_timeframe)),
        )
    if parent_timeframe is None:
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=False,
            reason_codes=("parent_evidence_timeframe_missing",),
            reason="Subdivision proof requires the parent evidence timeframe.",
            parent_degree=parent_degree_text,
            parent_timeframe=None,
        )
    try:
        canonical_parent = normalize_timeframe_name(parent_timeframe)
    except ValueError as exc:
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=False,
            reason_codes=("parent_evidence_timeframe_invalid",),
            reason=str(exc),
            parent_degree=parent_degree_text,
            parent_timeframe=str(parent_timeframe),
        )
    if not timeframe_is_finer(canonical, canonical_parent):
        return PurposeAwareTimeframeCompatibility(
            degree=degree_text,
            timeframe=canonical,
            purpose=purpose_value,
            compatible=False,
            reason_codes=("proof_timeframe_not_finer",),
            reason="Subdivision proof evidence must be finer than parent evidence.",
            parent_degree=parent_degree_text,
            parent_timeframe=canonical_parent,
        )
    return PurposeAwareTimeframeCompatibility(
        degree=degree_text,
        timeframe=canonical,
        purpose=purpose_value,
        compatible=True,
        reason_codes=(),
        reason=(
            "The child descends exactly one Elliott degree and uses a strictly "
            "finer evidence timeframe; the evidence timeframe does not assign degree."
        ),
        parent_degree=parent_degree_text,
        parent_timeframe=canonical_parent,
    )


@dataclass(frozen=True, slots=True)
class ProviderTimeframeCapability:
    canonical_name: str
    duration_seconds: int
    provider_alias: str
    classification: TimeframeClass
    native_available: bool
    derived: bool
    provider: str
    session_policy: str
    adjustment_policy: str
    availability: TimeframeAvailability = TimeframeAvailability.AVAILABLE
    schema_version: str = PROVIDER_TIMEFRAME_CAPABILITY_SCHEMA_VERSION
    content_hash: str = ""
    market_session_mode: MarketSessionMode = MarketSessionMode.UNKNOWN
    market_calendar_id: str | None = None
    market_calendar_source: str | None = None
    market_calendar_version: str | None = None
    market_timezone: str | None = None
    session_start_local: str | None = None
    session_end_local: str | None = None
    session_minutes: int | None = None
    provider_bar_alignment: ProviderBarAlignment = ProviderBarAlignment.UNKNOWN
    shortened_final_bar_emitted: bool | None = None

    def __post_init__(self) -> None:
        canonical = normalize_timeframe_name(self.canonical_name)
        object.__setattr__(self, "canonical_name", canonical)
        expected_duration = timeframe_duration_seconds(canonical)
        if self.duration_seconds != expected_duration:
            raise ValueError(
                "duration_seconds must match the canonical timeframe duration."
            )
        object.__setattr__(self, "provider_alias", _require_text(self.provider_alias, field_name="provider_alias"))
        expected_class = timeframe_class(canonical)
        classification = TimeframeClass(self.classification)
        if classification is not expected_class:
            raise ValueError("classification does not match timeframe granularity.")
        object.__setattr__(self, "classification", classification)
        if not isinstance(self.native_available, bool) or not isinstance(self.derived, bool):
            raise TypeError("native_available and derived must be boolean.")
        if self.native_available and self.derived:
            raise ValueError("A capability cannot be both provider-native and derived.")
        for name in ("provider", "session_policy", "adjustment_policy", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "availability", TimeframeAvailability(self.availability))
        object.__setattr__(
            self,
            "market_session_mode",
            MarketSessionMode(self.market_session_mode),
        )
        object.__setattr__(
            self,
            "provider_bar_alignment",
            ProviderBarAlignment(self.provider_bar_alignment),
        )
        for name in (
            "market_calendar_id",
            "market_calendar_source",
            "market_calendar_version",
            "market_timezone",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _require_text(value, field_name=name),
                )
        for name in ("session_start_local", "session_end_local"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _local_time_text(value, field_name=name),
                )
        if self.session_minutes is not None and (
            not isinstance(self.session_minutes, int)
            or isinstance(self.session_minutes, bool)
            or not 1 <= self.session_minutes <= 1440
        ):
            raise ValueError("session_minutes must be between 1 and 1440.")
        if self.shortened_final_bar_emitted is not None and not isinstance(
            self.shortened_final_bar_emitted,
            bool,
        ):
            raise TypeError("shortened_final_bar_emitted must be boolean or null.")
        if self.market_session_mode in {
            MarketSessionMode.REGULAR_SESSION_EQUITY,
            MarketSessionMode.EXTENDED_SESSION_EQUITY,
        } and (
            self.session_start_local is None
            or self.session_end_local is None
            or self.session_minutes is None
        ):
            raise ValueError(
                "Session-limited equity metadata requires local boundaries and session_minutes."
            )
        if self.content_hash and self.content_hash != provider_timeframe_capability_content_hash(self):
            raise ValueError("ProviderTimeframeCapability content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ProviderTimeframeCapability":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        canonical = normalize_timeframe_name(values["canonical_name"])
        values["canonical_name"] = canonical
        values.setdefault("duration_seconds", timeframe_duration_seconds(canonical))
        values.setdefault("classification", timeframe_class(canonical))
        result = cls(**values)
        return replace(result, content_hash=provider_timeframe_capability_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ProviderTimeframeCapability":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != provider_timeframe_capability_content_hash(result):
            raise ValueError("ProviderTimeframeCapability content_hash does not match its payload.")
        return result

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "canonical_name": self.canonical_name,
            "duration_seconds": self.duration_seconds,
            "provider_alias": self.provider_alias,
            "classification": self.classification.value,
            "native_available": self.native_available,
            "derived": self.derived,
            "provider": self.provider,
            "session_policy": self.session_policy,
            "adjustment_policy": self.adjustment_policy,
            "availability": self.availability.value,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "market_session_mode": self.market_session_mode.value,
            "market_calendar_id": self.market_calendar_id,
            "market_calendar_source": self.market_calendar_source,
            "market_calendar_version": self.market_calendar_version,
            "market_timezone": self.market_timezone,
            "session_start_local": self.session_start_local,
            "session_end_local": self.session_end_local,
            "session_minutes": self.session_minutes,
            "provider_bar_alignment": self.provider_bar_alignment.value,
            "shortened_final_bar_emitted": self.shortened_final_bar_emitted,
        }
        if self.schema_version in _LEGACY_PROVIDER_TIMEFRAME_CAPABILITY_SCHEMA_VERSIONS:
            for name in (
                "market_session_mode",
                "market_calendar_id",
                "market_calendar_source",
                "market_calendar_version",
                "market_timezone",
                "session_start_local",
                "session_end_local",
                "session_minutes",
                "provider_bar_alignment",
                "shortened_final_bar_emitted",
            ):
                payload.pop(name, None)
        return payload


def provider_timeframe_capability_content_hash(
    value: ProviderTimeframeCapability | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ProviderTimeframeCapability) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySet:
    provider: str
    capabilities: tuple[ProviderTimeframeCapability, ...]
    discovery_status: str
    source_interface: str
    discovered_at_utc: str
    limitations: tuple[str, ...] = ()
    schema_version: str = PROVIDER_CAPABILITY_SET_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("provider", "discovery_status", "source_interface", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(
            self,
            "discovered_at_utc",
            _utc_text(self.discovered_at_utc, field_name="discovered_at_utc"),
        )
        capabilities = tuple(
            item
            if isinstance(item, ProviderTimeframeCapability)
            else ProviderTimeframeCapability.from_dict(item)
            for item in self.capabilities
        )
        names = [item.canonical_name for item in capabilities]
        aliases = [item.provider_alias for item in capabilities]
        if len(names) != len(set(names)) or len(aliases) != len(set(aliases)):
            raise ValueError("Provider capability names and aliases must be unique.")
        if any(item.provider != self.provider for item in capabilities):
            raise ValueError("Every capability must use the capability-set provider.")
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted(capabilities, key=lambda item: timeframe_sort_key(item.canonical_name))),
        )
        object.__setattr__(self, "limitations", tuple(str(item) for item in self.limitations))
        if self.content_hash and self.content_hash != provider_capability_set_content_hash(self):
            raise ValueError("ProviderCapabilitySet content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "ProviderCapabilitySet":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=provider_capability_set_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ProviderCapabilitySet":
        result = cls(**dict(value))
        if verify_hash and result.content_hash != provider_capability_set_content_hash(result):
            raise ValueError("ProviderCapabilitySet content_hash does not match its payload.")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capabilities": [item.to_dict() for item in self.capabilities],
            "discovery_status": self.discovery_status,
            "source_interface": self.source_interface,
            "discovered_at_utc": self.discovered_at_utc,
            "limitations": list(self.limitations),
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }

    def available(self, timeframe: Any, *, native_only: bool = True) -> ProviderTimeframeCapability | None:
        canonical = normalize_timeframe_name(timeframe)
        return next(
            (
                item
                for item in self.capabilities
                if item.canonical_name == canonical
                and item.availability is TimeframeAvailability.AVAILABLE
                and (item.native_available or not native_only)
            ),
            None,
        )

    @property
    def supported_timeframes(self) -> tuple[str, ...]:
        return tuple(
            item.canonical_name
            for item in self.capabilities
            if item.availability is TimeframeAvailability.AVAILABLE
        )


def provider_capability_set_content_hash(
    value: ProviderCapabilitySet | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, ProviderCapabilitySet) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


EXAMPLE_CANONICAL_TIMEFRAMES = (
    "monthly",
    "weekly",
    "3d",
    "2d",
    "daily",
    "12h",
    "8h",
    "6h",
    "4h",
    "3h",
    "2h",
    "1h",
    "45m",
    "30m",
    "15m",
    "5m",
)


__all__ = [
    "DEGREE_TIMEFRAME_POLICY_VERSION",
    "EXAMPLE_CANONICAL_TIMEFRAMES",
    "PURPOSE_AWARE_TIMEFRAME_POLICY_VERSION",
    "PROVIDER_CAPABILITY_SET_SCHEMA_VERSION",
    "PROVIDER_TIMEFRAME_CAPABILITY_SCHEMA_VERSION",
    "TIMEFRAME_SCHEMA_VERSION",
    "DegreeTimeframeCompatibility",
    "MarketSessionMode",
    "PurposeAwareTimeframeCompatibility",
    "ProviderCapabilitySet",
    "ProviderBarAlignment",
    "ProviderTimeframeCapability",
    "TimeframeAvailability",
    "TimeframeClass",
    "TimeframeUsePurpose",
    "degree_timeframe_compatibility",
    "normalize_timeframe_name",
    "provider_capability_set_content_hash",
    "provider_timeframe_capability_content_hash",
    "sort_timeframes",
    "timeframe_class",
    "timeframe_duration_seconds",
    "timeframe_is_compatible_with_degree",
    "timeframe_compatibility_for_purpose",
    "timeframe_is_finer",
    "timeframe_is_same_or_finer",
    "timeframe_sort_key",
]
