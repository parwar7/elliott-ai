"""Cloud market-data snapshots for the Elliott analysis pipeline.

This module only acquires and normalizes OHLCV data. It deliberately does not
perform Elliott labeling, indicator interpretation, or trade reasoning.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .market_data_identity import build_feed_family, build_stream_identity
from .timeframes import (
    MarketSessionMode,
    ProviderBarAlignment,
    ProviderCapabilitySet,
    ProviderTimeframeCapability,
    normalize_timeframe_name,
)


TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
DEFAULT_ANALYSIS_TIMEFRAMES = (
    "monthly",
    "weekly",
    "daily",
    "4h",
    "1h",
    "15m",
)
TIMEFRAME_SPECIFICATIONS: dict[str, tuple[str, int]] = {
    "monthly": ("1month", 1200),
    "weekly": ("1week", 3000),
    "daily": ("1day", 5000),
    "4h": ("4h", 3000),
    "1h": ("1h", 3000),
    "15m": ("15min", 3000),
}
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._/\-]{0,39}$")
_EXCHANGE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._\-]{0,19}$")


class MarketDataProviderError(RuntimeError):
    """Raised when a cloud market-data snapshot cannot be produced safely."""


@dataclass(frozen=True)
class NormalizedMarketSymbol:
    requested_symbol: str
    canonical_symbol: str
    provider_symbol: str
    exchange: str | None
    file_slug: str


@dataclass(frozen=True)
class MarketDataBundle:
    requested_symbol: str
    canonical_symbol: str
    provider_symbol: str
    exchange: str | None
    captured_at: str
    paths: tuple[str, ...]
    available_timeframes: tuple[str, ...]
    unavailable_timeframes: tuple[str, ...]
    warnings: tuple[str, ...]
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_market_symbol(value: str) -> NormalizedMarketSymbol:
    """Normalize a user symbol without guessing cross-asset aliases."""
    raw = str(value or "").strip().upper()
    if not raw:
        raise ValueError("A market symbol is required.")
    if any(character.isspace() for character in raw):
        raise ValueError("Market symbols cannot contain whitespace.")

    exchange: str | None = None
    provider_symbol = raw
    if ":" in raw:
        exchange, provider_symbol = raw.split(":", 1)
        if not exchange or not provider_symbol or ":" in provider_symbol:
            raise ValueError("Use one optional EXCHANGE:SYMBOL prefix.")
        if not _EXCHANGE_PATTERN.fullmatch(exchange):
            raise ValueError("The exchange contains unsupported characters.")

    if not _SYMBOL_PATTERN.fullmatch(provider_symbol):
        raise ValueError(
            "The symbol contains unsupported characters. Examples: MSFT, "
            "NASDAQ:MSFT, BTC/USD, or XAU/USD."
        )
    canonical = f"{exchange}:{provider_symbol}" if exchange else provider_symbol
    slug = re.sub(r"[^A-Z0-9]+", "_", canonical).strip("_").lower()
    return NormalizedMarketSymbol(
        requested_symbol=raw,
        canonical_symbol=canonical,
        provider_symbol=provider_symbol,
        exchange=exchange,
        file_slug=slug,
    )


def normalize_timeframes(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    requested = tuple(
        dict.fromkeys(normalize_timeframe_name(value) for value in (values or ()))
    )
    if not requested:
        return DEFAULT_ANALYSIS_TIMEFRAMES
    unsupported = sorted(set(requested) - set(TIMEFRAME_SPECIFICATIONS))
    if unsupported:
        raise ValueError("Unsupported cloud timeframes: " + ", ".join(unsupported))
    return requested


def twelve_data_timeframe_capabilities(
    *, discovered_at_utc: str, symbol: str | None = None
) -> ProviderCapabilitySet:
    """Describe only intervals implemented by this repository's adapter.

    This is a local adapter capability declaration, not a claim about every
    interval Twelve Data may offer under every account plan.
    """

    normalized_symbol = normalize_market_symbol(symbol) if symbol else None
    is_nasdaq = bool(
        normalized_symbol
        and normalized_symbol.exchange in {"NASDAQ", "XNAS"}
    )

    def capability(timeframe: str, provider_alias: str) -> ProviderTimeframeCapability:
        values: dict[str, Any] = {
            "canonical_name": timeframe,
            "provider_alias": provider_alias,
            "native_available": True,
            "derived": False,
            "provider": "twelve_data",
            "session_policy": "regular_session_prepost_false",
            "adjustment_policy": "split_adjusted_dividend_unadjusted",
        }
        if is_nasdaq:
            alignment = (
                ProviderBarAlignment.CALENDAR_BUCKET
                if timeframe in {"monthly", "weekly"}
                else ProviderBarAlignment.SESSION_DAILY
                if timeframe == "daily"
                else ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
            )
            values.update(
                {
                    "market_session_mode": MarketSessionMode.REGULAR_SESSION_EQUITY,
                    "market_calendar_id": "XNAS",
                    "market_calendar_source": "elliott_ai.xnas_rules",
                    "market_calendar_version": "1.0.0",
                    "market_timezone": "America/New_York",
                    "session_start_local": "09:30",
                    "session_end_local": "16:00",
                    "session_minutes": 390,
                    "provider_bar_alignment": alignment,
                    "shortened_final_bar_emitted": (
                        alignment
                        is ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
                    ),
                }
            )
        return ProviderTimeframeCapability.create(**values)

    return ProviderCapabilitySet.create(
        provider="twelve_data",
        capabilities=tuple(
            capability(timeframe, provider_alias)
            for timeframe, (provider_alias, _output_size) in TIMEFRAME_SPECIFICATIONS.items()
        ),
        discovery_status="configured_adapter",
        source_interface="elliott_ai.live_market_data.TwelveDataClient",
        discovered_at_utc=discovered_at_utc,
        limitations=(
            "Capabilities describe repository adapter support; plan availability is verified only by a real request.",
            *(
                (
                    "XNAS session estimates use the versioned repository calendar policy; actual fetched counts remain authoritative.",
                )
                if is_nasdaq
                else (
                    "Market-session metadata is unavailable until an exchange-qualified symbol is supplied.",
                )
            ),
        ),
    )


class TwelveDataClient:
    """Small dependency-free client for Twelve Data time-series snapshots."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TWELVE_DATA_BASE_URL,
        timeout_seconds: int = 45,
        maximum_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not str(api_key or "").strip():
            raise ValueError("TWELVE_DATA_API_KEY is required.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if maximum_retries < 0 or maximum_retries > 5:
            raise ValueError("maximum_retries must be between 0 and 5.")
        self._api_key = str(api_key).strip()
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.maximum_retries = maximum_retries
        self._sleep = sleep

    def _request_json(self, endpoint: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}?{urlencode(parameters)}"
        request = Request(
            url,
            headers={
                "Authorization": f"apikey {self._api_key}",
                "Accept": "application/json",
                "User-Agent": "elliott-ai-cloud/1.0",
            },
            method="GET",
        )
        transient_codes = {429, 500, 502, 503, 504}
        for attempt in range(self.maximum_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise MarketDataProviderError(
                        "Twelve Data returned a non-object JSON response."
                    )
                return payload
            except HTTPError as exc:
                if exc.code in transient_codes and attempt < self.maximum_retries:
                    self._sleep(float(2**attempt))
                    continue
                raise MarketDataProviderError(
                    f"Twelve Data HTTP request failed with status {exc.code}."
                ) from exc
            except URLError as exc:
                if attempt < self.maximum_retries:
                    self._sleep(float(2**attempt))
                    continue
                raise MarketDataProviderError(
                    "Twelve Data could not be reached after bounded retries."
                ) from exc
            except json.JSONDecodeError as exc:
                raise MarketDataProviderError(
                    "Twelve Data returned malformed JSON."
                ) from exc
        raise MarketDataProviderError("Twelve Data request failed.")

    def fetch_time_series(
        self,
        symbol: NormalizedMarketSymbol,
        timeframe: str,
    ) -> dict[str, Any]:
        normalized_timeframe = normalize_timeframes((timeframe,))[0]
        interval, output_size = TIMEFRAME_SPECIFICATIONS[normalized_timeframe]
        parameters: dict[str, Any] = {
            "symbol": symbol.provider_symbol,
            "interval": interval,
            "outputsize": output_size,
            "order": "asc",
            "format": "JSON",
            "adjust": "splits",
            "prepost": "false",
        }
        if symbol.exchange:
            parameters["exchange"] = symbol.exchange
        if normalized_timeframe in {"4h", "1h", "15m"}:
            parameters["timezone"] = "UTC"

        payload = self._request_json("time_series", parameters)
        if str(payload.get("status", "")).lower() == "error":
            message = str(payload.get("message") or "provider rejected the request")
            raise MarketDataProviderError(f"Twelve Data error: {message}")
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise MarketDataProviderError(
                f"Twelve Data returned no {normalized_timeframe} candles for "
                f"{symbol.canonical_symbol}."
            )
        rows = [dict(row) for row in values if isinstance(row, Mapping)]
        if not rows:
            raise MarketDataProviderError(
                f"Twelve Data returned malformed {normalized_timeframe} candles."
            )
        rows.sort(key=lambda row: str(row.get("datetime", "")))
        metadata = payload.get("meta")
        return {
            "timeframe": normalized_timeframe,
            "provider_interval": interval,
            "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
            "values": rows,
        }

    def fetch_bundle(
        self,
        symbol_value: str,
        output_root: Path,
        *,
        timeframes: tuple[str, ...] | list[str] | None = None,
        captured_at: datetime | None = None,
    ) -> MarketDataBundle:
        symbol = normalize_market_symbol(symbol_value)
        requested_timeframes = normalize_timeframes(timeframes)
        captured = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        captured_text = captured.isoformat(timespec="seconds")
        directory = Path(output_root).resolve() / (
            f"{symbol.file_slug}_{captured.strftime('%Y%m%dT%H%M%SZ')}"
        )
        directory.mkdir(parents=True, exist_ok=True)

        paths: list[str] = []
        available: list[str] = []
        unavailable: list[str] = []
        warnings: list[str] = []
        capability_set = twelve_data_timeframe_capabilities(
            discovered_at_utc=captured_text,
            symbol=symbol.canonical_symbol,
        )
        for timeframe in requested_timeframes:
            try:
                series = self.fetch_time_series(symbol, timeframe)
            except MarketDataProviderError as exc:
                unavailable.append(timeframe)
                warnings.append(f"{timeframe}: {exc}")
                continue

            provider_metadata = series["metadata"]
            candles = [
                {
                    "date": row.get("datetime"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                }
                for row in series["values"]
            ]
            exchange = str(
                provider_metadata.get("exchange")
                or symbol.exchange
                or "unknown"
            )
            provider_symbol = str(
                provider_metadata.get("symbol") or symbol.provider_symbol
            )
            feed_family = build_feed_family(
                provider="twelve_data",
                source_interface="elliott_ai.live_market_data.TwelveDataClient",
                canonical_symbol=symbol.canonical_symbol,
                provider_symbol=provider_symbol,
                exchange=exchange,
                mic=(
                    str(provider_metadata.get("mic_code"))
                    if provider_metadata.get("mic_code")
                    else None
                ),
                session_policy="regular_session_prepost_false",
                timezone=str(
                    provider_metadata.get("exchange_timezone")
                    or ("America/New_York" if exchange in {"NASDAQ", "XNAS"} else "unknown")
                ),
                price_adjustment="split_adjusted_dividend_unadjusted",
                dividend_adjustment="unadjusted",
                volume_adjustment="split_adjusted",
                price_basis="split-adjusted dividend-unadjusted OHLCV",
            )
            capability = capability_set.available(timeframe)
            if capability is None:
                raise MarketDataProviderError(
                    f"No configured Twelve Data capability exists for {timeframe}."
                )
            stream_identity = build_stream_identity(
                feed_family_hash=feed_family.feed_family_hash,
                canonical_timeframe=timeframe,
                provider_interval_alias=series["provider_interval"],
                timestamp_semantics="bar_open",
                provider_bar_alignment=capability.provider_bar_alignment,
                native=True,
                derived=False,
            )
            snapshot_payload: dict[str, Any] = {
                "metadata": {
                    "source_mode": "cloud_api_snapshot",
                    "provider": "twelve_data",
                    "requested_symbol": symbol.requested_symbol,
                    "provider_symbol": symbol.provider_symbol,
                    "resolved_symbol": provider_metadata.get(
                        "symbol", symbol.provider_symbol
                    ),
                    "exchange": provider_metadata.get(
                        "exchange", symbol.exchange or "unknown"
                    ),
                    "mic_code": provider_metadata.get("mic_code"),
                    "asset_type": provider_metadata.get("type"),
                    "currency": provider_metadata.get("currency"),
                    "exchange_timezone": provider_metadata.get(
                        "exchange_timezone"
                    ),
                    "timeframe": timeframe,
                    "provider_interval": series["provider_interval"],
                    "adjustment": "splits",
                    "session": "regular",
                    "captured_at": captured_text,
                    "applicable_cutoff": candles[-1]["date"],
                    "feed_identity": "|".join(
                        (
                            "twelve_data",
                            str(provider_metadata.get("exchange") or symbol.exchange or "unknown"),
                            str(provider_metadata.get("symbol") or symbol.provider_symbol),
                            timeframe,
                            "splits",
                            "regular",
                        )
                    ),
                    "feed_family": feed_family.to_dict(),
                    "feed_family_hash": feed_family.feed_family_hash,
                    "stream_identity": stream_identity.to_dict(),
                    "stream_hash": stream_identity.stream_hash,
                },
                "candles": candles,
            }
            snapshot_payload["metadata"]["source_document_hash"] = _content_hash(
                snapshot_payload
            )
            path = directory / f"tv_{symbol.file_slug}_{timeframe}.json"
            path.write_text(
                json.dumps(snapshot_payload, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
            paths.append(str(path))
            available.append(timeframe)

        macro_available = bool({"monthly", "weekly"} & set(available))
        if "daily" not in available or not macro_available:
            raise MarketDataProviderError(
                "A cloud analysis requires daily data and at least one monthly or weekly "
                "dataset. Available timeframes: " + (", ".join(available) or "none")
            )

        hash_payload = {
            "requested_symbol": symbol.requested_symbol,
            "canonical_symbol": symbol.canonical_symbol,
            "provider_symbol": symbol.provider_symbol,
            "exchange": symbol.exchange,
            "captured_at": captured_text,
            "paths": paths,
            "available_timeframes": available,
            "unavailable_timeframes": unavailable,
            "warnings": warnings,
        }
        return MarketDataBundle(
            requested_symbol=symbol.requested_symbol,
            canonical_symbol=symbol.canonical_symbol,
            provider_symbol=symbol.provider_symbol,
            exchange=symbol.exchange,
            captured_at=captured_text,
            paths=tuple(paths),
            available_timeframes=tuple(available),
            unavailable_timeframes=tuple(unavailable),
            warnings=tuple(warnings),
            content_hash=_content_hash(hash_payload),
        )
