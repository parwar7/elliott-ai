"""Typed market-data lineage and interval-stream identities.

Feed-family identity answers whether two datasets come from the same comparable
underlying source.  Stream identity answers whether they are the same native or
derived candle stream.  The distinction is important for recursive Elliott
proof, where a native 4h parent and a native 1h child should share a feed family
but must not claim the same stream identity.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any, Mapping

from .forecast_records import canonical_sha256
from .timeframes import ProviderBarAlignment, normalize_timeframe_name


MARKET_DATA_FEED_FAMILY_SCHEMA_VERSION = "market-data-feed-family-1.0.0"
MARKET_DATA_STREAM_IDENTITY_SCHEMA_VERSION = "market-data-stream-identity-1.0.0"


class StreamTimestampSemantics(StrEnum):
    BAR_OPEN = "bar_open"
    BAR_CLOSE = "bar_close"
    PERIOD_START = "period_start"
    PERIOD_END = "period_end"
    UNKNOWN = "unknown"


class MarketDataStreamKind(StrEnum):
    NATIVE = "native"
    DERIVED = "derived"


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name=field_name)


def _hash(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return text


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
class MarketDataFeedFamily(_Contract):
    provider: str
    source_interface: str
    canonical_symbol: str
    provider_symbol: str
    exchange: str
    mic: str | None
    session_policy: str
    timezone: str
    price_adjustment: str
    dividend_adjustment: str
    volume_adjustment: str
    price_basis: str
    schema_version: str = MARKET_DATA_FEED_FAMILY_SCHEMA_VERSION
    feed_family_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "provider",
            "source_interface",
            "canonical_symbol",
            "provider_symbol",
            "exchange",
            "session_policy",
            "timezone",
            "price_adjustment",
            "dividend_adjustment",
            "volume_adjustment",
            "price_basis",
            "schema_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        object.__setattr__(self, "mic", _optional_text(self.mic, field_name="mic"))
        if self.feed_family_hash:
            supplied = _hash(self.feed_family_hash, field_name="feed_family_hash")
            if supplied != market_data_feed_family_hash(self):
                raise ValueError("MarketDataFeedFamily feed_family_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "MarketDataFeedFamily":
        if values.pop("feed_family_hash", ""):
            raise ValueError("create() calculates feed_family_hash; do not supply it.")
        result = cls(**values)
        return replace(result, feed_family_hash=market_data_feed_family_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "MarketDataFeedFamily":
        result = cls(**dict(value))
        if verify_hash and result.feed_family_hash != market_data_feed_family_hash(result):
            raise ValueError("MarketDataFeedFamily feed_family_hash does not match its payload.")
        return result


def market_data_feed_family_hash(
    value: MarketDataFeedFamily | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, MarketDataFeedFamily) else dict(value)
    payload.pop("feed_family_hash", None)
    return canonical_sha256(payload)


def canonical_timestamp_semantics(value: Any) -> StreamTimestampSemantics:
    text = str(value or "").strip().casefold()
    if "bar_open" in text:
        return StreamTimestampSemantics.BAR_OPEN
    if "period_start" in text:
        return StreamTimestampSemantics.PERIOD_START
    if "bar_close" in text:
        return StreamTimestampSemantics.BAR_CLOSE
    if "period_end" in text:
        return StreamTimestampSemantics.PERIOD_END
    return StreamTimestampSemantics.UNKNOWN


def canonical_bar_alignment(
    value: Any, *, timestamp_semantics: Any = None
) -> ProviderBarAlignment:
    if value is not None and str(value).strip():
        try:
            return ProviderBarAlignment(str(value).strip())
        except ValueError:
            pass
    text = str(timestamp_semantics or "").strip().casefold()
    if "session_open_aligned_short_final" in text or "session_open_aligned_with_short_final" in text:
        return ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
    if "session_open_aligned_full_only" in text:
        return ProviderBarAlignment.SESSION_OPEN_ALIGNED_FULL_ONLY
    if "calendar_bucket" in text:
        return ProviderBarAlignment.CALENDAR_BUCKET
    if "session_daily" in text:
        return ProviderBarAlignment.SESSION_DAILY
    if "elapsed_time" in text:
        return ProviderBarAlignment.ELAPSED_TIME
    return ProviderBarAlignment.UNKNOWN


@dataclass(frozen=True, slots=True)
class MarketDataStreamIdentity(_Contract):
    feed_family_hash: str
    canonical_timeframe: str
    provider_interval_alias: str
    timestamp_semantics: StreamTimestampSemantics
    provider_bar_alignment: ProviderBarAlignment
    stream_kind: MarketDataStreamKind
    schema_version: str = MARKET_DATA_STREAM_IDENTITY_SCHEMA_VERSION
    stream_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "feed_family_hash", _hash(self.feed_family_hash, field_name="feed_family_hash"))
        object.__setattr__(self, "canonical_timeframe", normalize_timeframe_name(self.canonical_timeframe))
        object.__setattr__(self, "provider_interval_alias", _text(self.provider_interval_alias, field_name="provider_interval_alias"))
        object.__setattr__(self, "timestamp_semantics", StreamTimestampSemantics(self.timestamp_semantics))
        object.__setattr__(self, "provider_bar_alignment", ProviderBarAlignment(self.provider_bar_alignment))
        object.__setattr__(self, "stream_kind", MarketDataStreamKind(self.stream_kind))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.stream_hash:
            supplied = _hash(self.stream_hash, field_name="stream_hash")
            if supplied != market_data_stream_hash(self):
                raise ValueError("MarketDataStreamIdentity stream_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "MarketDataStreamIdentity":
        if values.pop("stream_hash", ""):
            raise ValueError("create() calculates stream_hash; do not supply it.")
        result = cls(**values)
        return replace(result, stream_hash=market_data_stream_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "MarketDataStreamIdentity":
        result = cls(**dict(value))
        if verify_hash and result.stream_hash != market_data_stream_hash(result):
            raise ValueError("MarketDataStreamIdentity stream_hash does not match its payload.")
        return result


def market_data_stream_hash(
    value: MarketDataStreamIdentity | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, MarketDataStreamIdentity) else dict(value)
    payload.pop("stream_hash", None)
    return canonical_sha256(payload)


def build_feed_family(
    *,
    provider: str,
    source_interface: str,
    canonical_symbol: str,
    provider_symbol: str,
    exchange: str,
    mic: str | None,
    session_policy: str,
    timezone: str,
    price_adjustment: str,
    dividend_adjustment: str,
    volume_adjustment: str,
    price_basis: str,
) -> MarketDataFeedFamily:
    return MarketDataFeedFamily.create(
        provider=provider,
        source_interface=source_interface,
        canonical_symbol=canonical_symbol,
        provider_symbol=provider_symbol,
        exchange=exchange,
        mic=mic,
        session_policy=session_policy,
        timezone=timezone,
        price_adjustment=price_adjustment,
        dividend_adjustment=dividend_adjustment,
        volume_adjustment=volume_adjustment,
        price_basis=price_basis,
    )


def build_stream_identity(
    *,
    feed_family_hash: str,
    canonical_timeframe: str,
    provider_interval_alias: str,
    timestamp_semantics: Any,
    provider_bar_alignment: Any = None,
    native: bool,
    derived: bool,
) -> MarketDataStreamIdentity:
    if bool(native) == bool(derived):
        raise ValueError("A stream must be exactly one of native or derived.")
    return MarketDataStreamIdentity.create(
        feed_family_hash=feed_family_hash,
        canonical_timeframe=canonical_timeframe,
        provider_interval_alias=provider_interval_alias,
        timestamp_semantics=canonical_timestamp_semantics(timestamp_semantics),
        provider_bar_alignment=canonical_bar_alignment(
            provider_bar_alignment,
            timestamp_semantics=timestamp_semantics,
        ),
        stream_kind=(
            MarketDataStreamKind.NATIVE if native else MarketDataStreamKind.DERIVED
        ),
    )


def _adjustment_component(adjustment: Any, *names: str) -> str:
    if isinstance(adjustment, Mapping):
        for name in names:
            value = adjustment.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return "unspecified"
    text = str(adjustment or "").strip()
    return text or "unspecified"


def feed_family_from_dataset_cutoff(dataset: Any) -> MarketDataFeedFamily:
    """Derive the strongest family available from legacy DatasetCutoff fields.

    DatasetCutoff predates source-interface and MIC fields.  Those unavailable
    values remain explicit rather than being guessed from its legacy string ID.
    """

    adjustment = getattr(dataset, "adjustment")
    return build_feed_family(
        provider=getattr(dataset, "provider"),
        source_interface="dataset_cutoff_source_interface_unavailable",
        canonical_symbol=getattr(dataset, "requested_symbol"),
        provider_symbol=getattr(dataset, "resolved_symbol"),
        exchange=getattr(dataset, "exchange"),
        mic=None,
        session_policy=getattr(dataset, "session"),
        timezone=getattr(dataset, "timezone"),
        price_adjustment=_adjustment_component(adjustment, "price", "requested"),
        dividend_adjustment=_adjustment_component(adjustment, "dividend", "dividends"),
        volume_adjustment=_adjustment_component(adjustment, "volume"),
        price_basis=getattr(dataset, "price_basis"),
    )


def dataset_feed_family_matches(left: Any, right: Any) -> bool:
    return (
        feed_family_from_dataset_cutoff(left).feed_family_hash
        == feed_family_from_dataset_cutoff(right).feed_family_hash
    )


def twelve_data_legacy_stream_identity(
    family: MarketDataFeedFamily, *, canonical_timeframe: str
) -> str | None:
    """Build the documented repository label from typed Twelve Data fields.

    This is intentionally provider-specific and never parses or edits an old
    label.  Unknown policies return ``None`` instead of inventing a token.
    """

    if family.provider.casefold() != "twelve_data":
        return None
    adjustment = family.price_adjustment.casefold().replace("-", "_")
    if "split" not in adjustment:
        return None
    session = family.session_policy.casefold()
    if "regular" not in session or "extended" in session:
        return None
    return "|".join(
        (
            "twelve_data",
            family.exchange,
            family.provider_symbol,
            normalize_timeframe_name(canonical_timeframe),
            "splits",
            "regular",
        )
    )


__all__ = [
    "MARKET_DATA_FEED_FAMILY_SCHEMA_VERSION",
    "MARKET_DATA_STREAM_IDENTITY_SCHEMA_VERSION",
    "MarketDataFeedFamily",
    "MarketDataStreamIdentity",
    "MarketDataStreamKind",
    "StreamTimestampSemantics",
    "build_feed_family",
    "build_stream_identity",
    "canonical_bar_alignment",
    "canonical_timestamp_semantics",
    "dataset_feed_family_matches",
    "feed_family_from_dataset_cutoff",
    "market_data_feed_family_hash",
    "market_data_stream_hash",
    "twelve_data_legacy_stream_identity",
]
