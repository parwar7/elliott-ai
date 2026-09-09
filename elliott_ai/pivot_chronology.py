"""Immutable chronology groups for catalog-backed price boundaries.

An OHLC candle records its high and low, but not which occurred first.  This
module keeps that missing intrabar ordering explicit and reusable across root
segmentation, child-graph generation, and deterministic subdivision proof.
It contains no Elliott-family logic, provider access, or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
import math
import re
from typing import Any, Mapping, Sequence

from .forecast_records import canonical_sha256
from .timeframes import normalize_timeframe_name


PIVOT_CHRONOLOGY_GROUP_SCHEMA_VERSION = "pivot-chronology-group-1.0.0"
PIVOT_CHRONOLOGY_MEMBER_SCHEMA_VERSION = "pivot-chronology-member-1.0.0"

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class IntrabarOrderStatus(StrEnum):
    SINGLE_MEMBER = "single_member"
    UNKNOWN = "unknown"
    RESOLVED_BY_FINER_NATIVE_DATA = "resolved_by_finer_native_data"


class ChronologyExtremum(StrEnum):
    HIGH = "high"
    LOW = "low"
    OBSERVATION = "observation"


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
            raise ValueError("Canonical chronology JSON cannot contain non-finite values.")
        return value
    raise TypeError(f"Unsupported chronology value: {type(value).__name__}.")


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


class _Contract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class PivotChronologyMember(_Contract):
    boundary_id: str
    extremum: ChronologyExtremum
    source_bundle_hash: str
    source_bar_hash: str
    source_candle_timestamp_utc: str
    timeframe: str
    schema_version: str = PIVOT_CHRONOLOGY_MEMBER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "boundary_id", _text(self.boundary_id, field_name="boundary_id"))
        object.__setattr__(self, "extremum", ChronologyExtremum(self.extremum))
        object.__setattr__(self, "source_bundle_hash", _hash(self.source_bundle_hash, field_name="source_bundle_hash"))
        object.__setattr__(self, "source_bar_hash", _hash(self.source_bar_hash, field_name="source_bar_hash"))
        object.__setattr__(
            self,
            "source_candle_timestamp_utc",
            _utc_text(
                self.source_candle_timestamp_utc,
                field_name="source_candle_timestamp_utc",
            ),
        )
        object.__setattr__(
            self,
            "timeframe",
            normalize_timeframe_name(_text(self.timeframe, field_name="timeframe")),
        )
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))


@dataclass(frozen=True, slots=True)
class PivotChronologyGroup(_Contract):
    chronology_group_id: str
    chronology_ordinal: int
    source_bundle_hash: str
    source_bar_hash: str
    source_candle_timestamp_utc: str
    timeframe: str
    member_boundary_ids: tuple[str, ...]
    member_extrema: tuple[ChronologyExtremum, ...]
    intrabar_order_status: IntrabarOrderStatus
    maximum_selectable_members: int
    schema_version: str = PIVOT_CHRONOLOGY_GROUP_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "chronology_group_id",
            _text(self.chronology_group_id, field_name="chronology_group_id"),
        )
        if (
            not isinstance(self.chronology_ordinal, int)
            or isinstance(self.chronology_ordinal, bool)
            or self.chronology_ordinal < 0
        ):
            raise ValueError("chronology_ordinal must be a nonnegative integer.")
        object.__setattr__(self, "source_bundle_hash", _hash(self.source_bundle_hash, field_name="source_bundle_hash"))
        object.__setattr__(self, "source_bar_hash", _hash(self.source_bar_hash, field_name="source_bar_hash"))
        object.__setattr__(
            self,
            "source_candle_timestamp_utc",
            _utc_text(
                self.source_candle_timestamp_utc,
                field_name="source_candle_timestamp_utc",
            ),
        )
        object.__setattr__(
            self,
            "timeframe",
            normalize_timeframe_name(_text(self.timeframe, field_name="timeframe")),
        )
        member_ids = tuple(
            _text(item, field_name="member_boundary_id")
            for item in self.member_boundary_ids
        )
        extrema = tuple(ChronologyExtremum(item) for item in self.member_extrema)
        if not member_ids or len(member_ids) != len(set(member_ids)):
            raise ValueError("member_boundary_ids must be non-empty and unique.")
        if len(member_ids) != len(extrema):
            raise ValueError("member_boundary_ids and member_extrema must align.")
        object.__setattr__(self, "member_boundary_ids", member_ids)
        object.__setattr__(self, "member_extrema", extrema)
        status = IntrabarOrderStatus(self.intrabar_order_status)
        object.__setattr__(self, "intrabar_order_status", status)
        if (
            not isinstance(self.maximum_selectable_members, int)
            or isinstance(self.maximum_selectable_members, bool)
            or self.maximum_selectable_members < 1
            or self.maximum_selectable_members > len(member_ids)
        ):
            raise ValueError("maximum_selectable_members is outside the group size.")
        if status is IntrabarOrderStatus.UNKNOWN and self.maximum_selectable_members != 1:
            raise ValueError("Unknown intrabar order permits at most one selected member.")
        if len(member_ids) > 1 and status is IntrabarOrderStatus.SINGLE_MEMBER:
            raise ValueError("A multi-member chronology group cannot be single_member.")
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.content_hash and self.content_hash != pivot_chronology_group_content_hash(self):
            raise ValueError("PivotChronologyGroup content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "PivotChronologyGroup":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("chronology_group_id"):
            values["chronology_group_id"] = "chronology_group_" + canonical_sha256(
                {
                    "source_bundle_hash": _hash(
                        values.get("source_bundle_hash"),
                        field_name="source_bundle_hash",
                    ),
                    "source_bar_hash": _hash(
                        values.get("source_bar_hash"),
                        field_name="source_bar_hash",
                    ),
                    "source_candle_timestamp_utc": _utc_text(
                        values.get("source_candle_timestamp_utc"),
                        field_name="source_candle_timestamp_utc",
                    ),
                    "timeframe": normalize_timeframe_name(
                        _text(values.get("timeframe"), field_name="timeframe")
                    ),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=pivot_chronology_group_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "PivotChronologyGroup":
        raw = dict(value)
        raw["member_boundary_ids"] = tuple(raw.get("member_boundary_ids", ()))
        raw["member_extrema"] = tuple(raw.get("member_extrema", ()))
        result = cls(**raw)
        if verify_hash and result.content_hash != pivot_chronology_group_content_hash(result):
            raise ValueError("PivotChronologyGroup content_hash does not match its payload.")
        return result


def pivot_chronology_group_content_hash(
    value: PivotChronologyGroup | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, PivotChronologyGroup) else dict(value)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class PivotChronologyConflict(_Contract):
    chronology_group_id: str
    chronology_ordinal: int
    selected_boundary_ids: tuple[str, ...]
    source_bundle_hash: str
    source_bar_hash: str
    source_candle_timestamp_utc: str
    timeframe: str
    intrabar_order_status: IntrabarOrderStatus
    maximum_selectable_members: int


def build_pivot_chronology_groups(
    members: Sequence[PivotChronologyMember],
) -> tuple[PivotChronologyGroup, ...]:
    """Group catalog members by their immutable native source candle."""

    if not members:
        return ()
    buckets: dict[tuple[str, str, str, str], list[PivotChronologyMember]] = {}
    for member in members:
        if not isinstance(member, PivotChronologyMember):
            raise TypeError("members must contain PivotChronologyMember values.")
        key = (
            member.source_bundle_hash,
            member.source_bar_hash,
            member.source_candle_timestamp_utc,
            member.timeframe,
        )
        buckets.setdefault(key, []).append(member)
    ordered = sorted(
        buckets.items(),
        key=lambda item: (
            _utc(item[0][2]),
            item[0][3],
            item[0][0],
            item[0][1],
        ),
    )
    result: list[PivotChronologyGroup] = []
    for ordinal, (key, bucket) in enumerate(ordered):
        source_bundle_hash, source_bar_hash, timestamp, timeframe = key
        sorted_members = tuple(sorted(bucket, key=lambda item: item.boundary_id))
        status = (
            IntrabarOrderStatus.UNKNOWN
            if len(sorted_members) > 1
            else IntrabarOrderStatus.SINGLE_MEMBER
        )
        result.append(
            PivotChronologyGroup.create(
                chronology_group_id="",
                chronology_ordinal=ordinal,
                source_bundle_hash=source_bundle_hash,
                source_bar_hash=source_bar_hash,
                source_candle_timestamp_utc=timestamp,
                timeframe=timeframe,
                member_boundary_ids=tuple(
                    item.boundary_id for item in sorted_members
                ),
                member_extrema=tuple(item.extremum for item in sorted_members),
                intrabar_order_status=status,
                maximum_selectable_members=1,
            )
        )
    return tuple(result)


def chronology_membership(
    groups: Sequence[PivotChronologyGroup],
) -> dict[str, PivotChronologyGroup]:
    result: dict[str, PivotChronologyGroup] = {}
    for group in groups:
        for boundary_id in group.member_boundary_ids:
            if boundary_id in result:
                raise ValueError("A boundary cannot belong to two chronology groups.")
            result[boundary_id] = group
    return result


def first_chronology_conflict(
    ordered_boundary_ids: Sequence[str],
    groups: Sequence[PivotChronologyGroup],
) -> PivotChronologyConflict | None:
    """Return the first over-selected source candle without inferring order."""

    membership = chronology_membership(groups)
    selected_by_group: dict[str, list[str]] = {}
    group_by_id = {item.chronology_group_id: item for item in groups}
    for boundary_id in ordered_boundary_ids:
        group = membership.get(boundary_id)
        if group is None:
            continue
        selected = selected_by_group.setdefault(group.chronology_group_id, [])
        if boundary_id not in selected:
            selected.append(boundary_id)
        if len(selected) > group.maximum_selectable_members:
            return PivotChronologyConflict(
                chronology_group_id=group.chronology_group_id,
                chronology_ordinal=group.chronology_ordinal,
                selected_boundary_ids=tuple(selected),
                source_bundle_hash=group.source_bundle_hash,
                source_bar_hash=group.source_bar_hash,
                source_candle_timestamp_utc=group.source_candle_timestamp_utc,
                timeframe=group.timeframe,
                intrabar_order_status=group.intrabar_order_status,
                maximum_selectable_members=group.maximum_selectable_members,
            )
    return None


def chronology_groups_for_typed_pivots(
    pivots: Sequence[Any],
    *,
    source_bundle_hash: str,
) -> tuple[PivotChronologyGroup, ...]:
    """Create chronology groups from immutable typed-pivot-like values."""

    members = tuple(
        PivotChronologyMember(
            boundary_id=item.pivot_id,
            extremum=ChronologyExtremum(item.pivot_type.value),
            source_bundle_hash=source_bundle_hash,
            source_bar_hash=item.source_bar_hash,
            source_candle_timestamp_utc=item.timestamp_utc,
            timeframe=item.timeframe,
        )
        for item in pivots
    )
    return build_pivot_chronology_groups(members)


def child_graph_boundary_path(children: Sequence[Any]) -> tuple[str, ...]:
    """Return graph endpoints in authored order, collapsing exact connections."""

    path: list[str] = []
    for child in children:
        for pivot in (child.start_pivot, child.end_pivot):
            if not path or path[-1] != pivot.pivot_id:
                path.append(pivot.pivot_id)
    return tuple(path)


__all__ = [
    "ChronologyExtremum",
    "IntrabarOrderStatus",
    "PIVOT_CHRONOLOGY_GROUP_SCHEMA_VERSION",
    "PIVOT_CHRONOLOGY_MEMBER_SCHEMA_VERSION",
    "PivotChronologyConflict",
    "PivotChronologyGroup",
    "PivotChronologyMember",
    "build_pivot_chronology_groups",
    "child_graph_boundary_path",
    "chronology_groups_for_typed_pivots",
    "chronology_membership",
    "first_chronology_conflict",
    "pivot_chronology_group_content_hash",
]
