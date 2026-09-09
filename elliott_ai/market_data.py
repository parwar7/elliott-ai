"""Dependency-free inspection of TradingView OHLCV exports."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .indicators import (
    DEFAULT_RSI_PERIOD,
    RSI_METHOD_ID,
    enrich_candles,
    normalize_features,
    summarize_indicators,
)
from .timeframes import (
    EXAMPLE_CANONICAL_TIMEFRAMES,
    normalize_timeframe_name,
    timeframe_class,
    timeframe_duration_seconds,
    timeframe_sort_key,
    TimeframeClass,
)


TIME_FIELDS = ("date", "datetime", "time", "timestamp", "timestamp_utc")
NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
VOLUME_MULTIPLIERS = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0, "T": 1_000_000_000_000.0}
INTRADAY_TIMEFRAMES = {
    item
    for item in EXAMPLE_CANONICAL_TIMEFRAMES
    if timeframe_class(item) is TimeframeClass.INTRADAY
}
FRESHNESS_HOURS = {
    "monthly": 24 * 40,
    "weekly": 24 * 10,
    "daily": 24 * 4,
    "4h": 24 * 4,
    "2h": 24 * 4,
    "1h": 24 * 4,
    "15m": 24 * 4,
    "5m": 24 * 4,
    "unknown": 24 * 4,
}
PIVOT_WINDOWS = {
    "monthly": (1, 2, 4),
    "weekly": (2, 6, 13),
    "daily": (3, 8, 21),
    "4h": (3, 8, 21),
    "2h": (3, 8, 21),
    "1h": (3, 8, 21),
    "15m": (3, 8, 21),
    "5m": (3, 8, 21),
    "unknown": (3, 8, 21),
}


def freshness_hours_for(timeframe: str) -> int:
    canonical = normalize_timeframe_name(timeframe)
    if canonical == "monthly":
        return 24 * 40
    if canonical == "weekly":
        return 24 * 10
    return 24 * 4


def pivot_windows_for(timeframe: str) -> tuple[int, ...]:
    canonical = normalize_timeframe_name(timeframe)
    duration = timeframe_duration_seconds(canonical)
    if duration >= timeframe_duration_seconds("monthly"):
        return (1, 2, 4)
    if duration >= timeframe_duration_seconds("weekly"):
        return (2, 6, 13)
    return (3, 8, 21)


def parse_number(value: Any, *, allow_suffix: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    raw = str(value).strip().upper().replace(",", "")
    match = NUMBER_PATTERN.search(raw)
    if match is None:
        return None
    number = float(match.group(0))
    if allow_suffix:
        suffix = next((letter for letter in reversed(raw) if letter in VOLUME_MULTIPLIERS), None)
        if suffix:
            number *= VOLUME_MULTIPLIERS[suffix]
    return number if math.isfinite(number) else None


def parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
    except ValueError:
        pass
    for pattern in (
        "%a %d %b '%y %H:%M",
        "%a %d %b %Y %H:%M",
        "%a %d %b '%y",
        "%a %d %b %Y",
        "%d %b '%y %H:%M",
        "%d %b %Y %H:%M",
        "%d %b '%y",
        "%d %b %Y",
        "%Y/%m/%d",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _find_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("candles", "bars", "ohlcv", "data", "records"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, Mapping)]
    return []


def _normalize_candle(row: Mapping[str, Any], position: int) -> dict[str, Any] | None:
    lowered = {str(key).lower(): value for key, value in row.items()}
    date_value = next((lowered[field] for field in TIME_FIELDS if field in lowered), None)
    high = parse_number(lowered.get("high"))
    low = parse_number(lowered.get("low"))
    close = parse_number(lowered.get("close"))
    if high is None or low is None or close is None:
        return None
    parsed_date = parse_date(date_value)
    return {
        "date": str(date_value) if date_value is not None else str(position),
        "parsed_date": parsed_date,
        "open": parse_number(lowered.get("open")),
        "high": high,
        "low": low,
        "close": close,
        "volume": parse_number(lowered.get("volume"), allow_suffix=True),
        "source_position": position,
    }


def load_candles(path: Path) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    candles = [
        candle
        for position, row in enumerate(_find_rows(value))
        if (candle := _normalize_candle(row, position)) is not None
    ]
    if not candles:
        raise ValueError(f"No valid OHLCV rows found in {path}.")
    if all(candle["parsed_date"] is not None for candle in candles):
        candles.sort(key=lambda candle: candle["parsed_date"])
    elif len(candles) > 1 and candles[0]["source_position"] < candles[-1]["source_position"]:
        # TradingView table exports are normally newest-first when dates cannot be parsed.
        candles.reverse()
    return candles


def load_market_metadata(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    if not isinstance(value, Mapping):
        return {}
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        return dict(metadata)
    # Adaptive NativeOHLCVBundle artifacts deliberately keep provenance at the
    # top level.  Read those immutable fields directly instead of rewriting the
    # bundle into a second market-data format.
    if str(value.get("schema_version", "")).startswith(
        ("adaptive-native-ohlcv-bundle-", "native-ohlcv-bundle-")
    ):
        return {
            "canonical_timeframe": value.get("timeframe"),
            "provider": value.get("provider"),
            "source_provider": value.get("provider"),
            "source_mode": "immutable_native_ohlcv_bundle",
            "feed_identity": value.get("feed_identity"),
            "requested_symbol": value.get("canonical_symbol"),
            "provider_symbol": value.get("provider_symbol"),
            "resolved_symbol": value.get("provider_symbol"),
            "exchange": value.get("exchange"),
            "session": value.get("session"),
            "chart_timezone": value.get("timezone"),
            "exchange_timezone": value.get("timezone"),
            "price_adjustment": value.get("price_adjustment"),
            "dividend_adjustment": value.get("dividend_adjustment"),
            "volume_adjustment": value.get("volume_adjustment"),
            "price_basis": value.get("price_basis"),
            "captured_at": value.get("acquisition_timestamp_utc"),
            "completed_candles_only": bool(value.get("incomplete_candles_excluded")),
            "native": value.get("native"),
            "derived": value.get("derived"),
            "requested_bar_count": None,
            "returned_bar_count": len(value.get("candles", ()))
            if isinstance(value.get("candles"), list)
            else None,
        }
    return {}


def add_ewo(candles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible entry point that now computes all candle features."""
    return enrich_candles(candles)


def _iso_date(candle: Mapping[str, Any]) -> str:
    parsed = candle.get("parsed_date")
    if isinstance(parsed, datetime):
        if parsed.time() == datetime.min.time():
            return parsed.date().isoformat()
        return parsed.isoformat()
    return str(candle.get("date", ""))


def _pivot_candidates(
    candles: list[dict[str, Any]], window: int, *, limit: int = 30
) -> list[dict[str, Any]]:
    if len(candles) < window * 2 + 1:
        return []
    candidates: list[dict[str, Any]] = []
    for index in range(window, len(candles) - window):
        segment = candles[index - window : index + window + 1]
        row = candles[index]
        if row["high"] == max(item["high"] for item in segment):
            candidates.append(
                {"type": "high", "date": _iso_date(row), "price": row["high"], "index": index}
            )
        if row["low"] == min(item["low"] for item in segment):
            candidates.append(
                {"type": "low", "date": _iso_date(row), "price": row["low"], "index": index}
            )
    candidates.sort(key=lambda pivot: pivot["index"])
    collapsed: list[dict[str, Any]] = []
    for pivot in candidates:
        if not collapsed or collapsed[-1]["type"] != pivot["type"]:
            collapsed.append(pivot)
            continue
        previous = collapsed[-1]
        more_extreme = (
            pivot["price"] > previous["price"]
            if pivot["type"] == "high"
            else pivot["price"] < previous["price"]
        )
        if more_extreme:
            collapsed[-1] = pivot
    if limit > 1 and len(collapsed) > limit:
        step = (len(collapsed) - 1) / (limit - 1)
        selected = {round(index * step) for index in range(limit)}
        collapsed = [pivot for index, pivot in enumerate(collapsed) if index in selected]
    for pivot in collapsed:
        pivot.pop("index", None)
    return collapsed


def _rounded(value: Any, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _compact_date(candle: Mapping[str, Any]) -> str:
    parsed = candle.get("parsed_date")
    if isinstance(parsed, datetime):
        if parsed.time() == datetime.min.time():
            return parsed.date().isoformat()
        return parsed.isoformat(timespec="minutes").replace("+00:00", "Z")
    return str(candle.get("date", ""))


def _feature_columns(features: Iterable[str] | None) -> list[str]:
    enabled = set(normalize_features(features))
    columns: list[str] = []
    if "volume" in enabled:
        columns.append("volume_ratio_50")
    if "ewo" in enabled:
        columns.extend(("ewo", "ewo_normalized_pct"))
    if "macd" in enabled:
        columns.extend(("macd", "macd_signal", "macd_histogram"))
    if "rsi" in enabled:
        columns.append("rsi")
    if "volatility" in enabled:
        columns.extend(("atr_pct", "bollinger_position", "keltner_position"))
    return columns


def _feature_values(candle: Mapping[str, Any], features: Iterable[str] | None) -> list[Any]:
    return [_rounded(candle.get(column), 4) for column in _feature_columns(features)]


def _compact_analysis_bar(
    candle: Mapping[str, Any], features: Iterable[str] | None = None
) -> list[Any]:
    return [
        _compact_date(candle),
        _rounded(candle.get("open"), 4),
        _rounded(candle.get("high"), 4),
        _rounded(candle.get("low"), 4),
        _rounded(candle.get("close"), 4),
        _rounded(candle.get("volume"), 0),
        *_feature_values(candle, features),
    ]


def _scaled_history_view(
    candles: list[dict[str, Any]], target_bars: int = 120, *, features: Iterable[str] | None = None
) -> list[list[Any]]:
    """Aggregate consecutive source bars into a compact full-history OHLCV path."""
    if not candles:
        return []
    bucket_count = min(max(1, target_bars), len(candles))
    aggregated: list[dict[str, Any]] = []
    for bucket in range(bucket_count):
        start_index = (bucket * len(candles)) // bucket_count
        end_index = ((bucket + 1) * len(candles)) // bucket_count
        rows = candles[start_index:max(start_index + 1, end_index)]
        volumes = [float(row["volume"]) for row in rows if row.get("volume") is not None]
        aggregated.append(
            {
                "date": rows[-1]["date"],
                "parsed_date": rows[-1].get("parsed_date"),
                "start_date": _iso_date(rows[0]),
                "open": rows[0].get("open"),
                "high": max(float(row["high"]) for row in rows),
                "low": min(float(row["low"]) for row in rows),
                "close": rows[-1]["close"],
                "volume": sum(volumes) if volumes else None,
                "source_bars": len(rows),
            }
        )
    enriched = enrich_candles(aggregated, features=features)
    return [
        [
            row["start_date"],
            _compact_date(row),
            _rounded(row.get("open"), 4),
            _rounded(row.get("high"), 4),
            _rounded(row.get("low"), 4),
            _rounded(row.get("close"), 4),
            _rounded(row.get("volume"), 0),
            *_feature_values(row, features),
            row["source_bars"],
        ]
        for row in enriched
    ]


def _pivot_ladders(
    candles: list[dict[str, Any]], timeframe: str, *, features: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    enabled = set(normalize_features(features))
    by_date = {_iso_date(candle): candle for candle in candles}
    ladders: list[dict[str, Any]] = []
    try:
        windows = pivot_windows_for(timeframe)
    except ValueError:
        windows = PIVOT_WINDOWS["unknown"]
    for window in windows:
        pivots = _pivot_candidates(candles, window, limit=36)
        compact_pivots: list[list[Any]] = []
        for pivot in pivots:
            candle = by_date.get(str(pivot.get("date")))
            if candle is None:
                continue
            values = [
                pivot.get("type"),
                _compact_date(candle),
                _rounded(pivot.get("price"), 4),
                _rounded(candle.get("volume"), 0),
            ]
            if "volume" in enabled:
                values.append(_rounded(candle.get("volume_ratio_50"), 4))
            if "ewo" in enabled:
                values.extend(
                    (
                        _rounded(candle.get("ewo"), 4),
                        _rounded(candle.get("ewo_normalized_pct"), 4),
                    )
                )
            if "macd" in enabled:
                values.append(_rounded(candle.get("macd_histogram"), 4))
            if "rsi" in enabled:
                values.append(_rounded(candle.get("rsi"), 4))
            if "volatility" in enabled:
                values.extend(
                    (
                        _rounded(candle.get("bollinger_position"), 4),
                        _rounded(candle.get("keltner_position"), 4),
                    )
                )
            compact_pivots.append(values)
        columns = ["type", "date", "price", "volume_at_bar"]
        if "volume" in enabled:
            columns.append("volume_ratio_50")
        if "ewo" in enabled:
            columns.extend(("ewo_endpoint", "ewo_normalized_pct"))
        if "macd" in enabled:
            columns.append("macd_histogram_endpoint")
        if "rsi" in enabled:
            columns.append("rsi_endpoint")
        if "volatility" in enabled:
            columns.extend(("bollinger_position", "keltner_position"))
        ladders.append(
            {
                "window_bars_each_side": window,
                "measurement": "native timeframe endpoint candidates",
                "columns": columns,
                "pivot_count": len(compact_pivots),
                "pivots": compact_pivots,
            }
        )
    return ladders


def infer_timeframe(path: Path) -> str:
    try:
        metadata = load_market_metadata(path)
    except (OSError, ValueError, json.JSONDecodeError):
        metadata = {}
    for key in ("canonical_timeframe", "timeframe", "interval"):
        if metadata.get(key):
            try:
                return normalize_timeframe_name(metadata[key])
            except ValueError:
                pass
    name = path.stem.lower()
    for token, label in (
        ("monthly", "monthly"),
        ("weekly", "weekly"),
        ("_3d", "3d"),
        ("_2d", "2d"),
        ("daily", "daily"),
        ("_12h", "12h"),
        ("_8h", "8h"),
        ("_6h", "6h"),
        ("_4h", "4h"),
        ("_3h", "3h"),
        ("_2h", "2h"),
        ("_1h", "1h"),
        ("_45m", "45m"),
        ("_30m", "30m"),
        ("_15m", "15m"),
        ("_5m", "5m"),
    ):
        if token in name:
            return label
    return "unknown"


def build_indicator_source_metadata(
    path: Path,
    metadata: Mapping[str, Any] | None = None,
    *,
    timeframe: str | None = None,
    price_field: str = "close",
    period: int = DEFAULT_RSI_PERIOD,
    calculation_method: str = RSI_METHOD_ID,
) -> dict[str, Any]:
    """Identify the exact source used for a same-feed indicator calculation."""
    source = dict(metadata or {})
    resolved = Path(path).resolve()
    resolved_timeframe = timeframe or infer_timeframe(resolved)
    provider = (
        source.get("provider")
        or source.get("source_provider")
        or source.get("source_mode")
        or "file_snapshot"
    )
    venue = source.get("exchange") or source.get("listed_exchange") or "unknown"
    symbol = (
        source.get("resolved_symbol")
        or source.get("chart_symbol")
        or source.get("provider_symbol")
        or source.get("requested_symbol")
        or resolved.stem
    )
    feed_identity = source.get("feed_identity") or "|".join(
        (
            str(provider),
            str(venue),
            str(symbol),
            resolved_timeframe,
            str(resolved),
        )
    )
    return {
        "provider": str(provider),
        "venue": str(venue),
        "symbol": str(symbol),
        "timeframe": resolved_timeframe,
        "price_field": price_field,
        "period": period,
        "calculation_method": calculation_method,
        "feed_identity": str(feed_identity),
        "source_path": str(resolved),
    }


def summarize_market_file(
    path: Path, *, features: Iterable[str] | None = None
) -> dict[str, Any]:
    path = Path(path).resolve()
    active_features = normalize_features(features)
    metadata = load_market_metadata(path)
    candles = enrich_candles(load_candles(path), features=active_features)
    timeframe = infer_timeframe(path)
    ewo_values = [float(row["ewo"]) for row in candles if row["ewo"] is not None]
    volumes = [float(row["volume"]) for row in candles if row["volume"] is not None]
    zero_crosses = 0
    for previous, current in zip(ewo_values, ewo_values[1:]):
        if (previous < 0 <= current) or (previous > 0 >= current):
            zero_crosses += 1
    window = max(2, min(12, len(candles) // 40))
    now = datetime.now(timezone.utc)
    last_bar = candles[-1].get("parsed_date")
    age_hours = (
        max(0.0, (now - last_bar).total_seconds() / 3600.0)
        if isinstance(last_bar, datetime)
        else None
    )
    try:
        freshness_limit = freshness_hours_for(timeframe)
    except ValueError:
        freshness_limit = FRESHNESS_HOURS["unknown"]
    technical_features = summarize_indicators(candles, features=active_features)
    if "rsi" in technical_features:
        technical_features["rsi"]["source"] = build_indicator_source_metadata(
            path, metadata, timeframe=timeframe
        )
    return {
        "source": str(path),
        "timeframe": timeframe,
        "bar_count": len(candles),
        "start_date": _iso_date(candles[0]),
        "end_date": _iso_date(candles[-1]),
        "snapshot": {
            "source_mode": metadata.get("source_mode", "file_snapshot"),
            "file_modified_utc": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "captured_at": metadata.get("captured_at"),
            "last_bar_age_hours": round(age_hours, 2) if age_hours is not None else None,
            "freshness_limit_hours": freshness_limit,
            "freshness_status": (
                "current_snapshot"
                if age_hours is not None and age_hours <= freshness_limit
                else "stale_or_unknown"
            ),
            "live_stream": False,
            "session": metadata.get("session", "unknown"),
            "adjustment_basis": {
                "dividends_adjusted": metadata.get("dividends_adjusted"),
            }
            if "dividends_adjusted" in metadata
            else "unknown",
            "chart_timezone": metadata.get("chart_timezone", "unknown"),
            "exchange_timezone": metadata.get("exchange_timezone", "unknown"),
            "requested_symbol": metadata.get("requested_symbol"),
            "chart_symbol": metadata.get("chart_symbol"),
            "provider_symbol": metadata.get("provider_symbol"),
            "resolved_symbol": metadata.get("resolved_symbol"),
            "exchange": metadata.get("exchange"),
            "listed_exchange": metadata.get("listed_exchange"),
            "volume_scope": metadata.get("volume_scope", "unknown"),
            "requested_bar_count": metadata.get("requested_bar_count"),
            "returned_bar_count": metadata.get("returned_bar_count"),
            "more_data_available": metadata.get("more_data_available"),
        },
        "price": {
            "first_close": candles[0]["close"],
            "last_close": candles[-1]["close"],
            "lowest_low": min(row["low"] for row in candles),
            "highest_high": max(row["high"] for row in candles),
        },
        "volume": {
            "available_bars": len(volumes),
            "cumulative": sum(volumes) if volumes else None,
            "average": sum(volumes) / len(volumes) if volumes else None,
        },
        "ewo": {
            "available_bars": len(ewo_values),
            "latest": ewo_values[-1] if ewo_values else None,
            "positive_peak": max(ewo_values) if ewo_values else None,
            "negative_peak": min(ewo_values) if ewo_values else None,
            "absolute_peak": max((abs(value) for value in ewo_values), default=None),
            "zero_crosses": zero_crosses,
        },
        "technical_features": technical_features,
        "price_pivot_candidates": {
            "method": f"strict local high/low, {window} bars on each side; candidates only",
            "pivots": _pivot_candidates(candles, window),
        },
        "analysis_views": {
            "scaled_full_history": {
                "method": (
                    "Consecutive native bars aggregated into at most 120 OHLCV bars. "
                    "Use for top-down structure and scale selection, not exact endpoints."
                ),
                "columns": [
                    "start",
                    "end",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    *_feature_columns(active_features),
                    "source_bars",
                ],
                "bar_count": min(120, len(candles)),
                "bars": _scaled_history_view(
                    candles, 120, features=active_features
                ),
            },
            "recent_native": {
                "method": (
                    "Last 120 unchanged native bars with same-timeframe EWO and volume."
                ),
                "columns": [
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    *_feature_columns(active_features),
                ],
                "bar_count": min(120, len(candles)),
                "bars": [
                    _compact_analysis_bar(candle, active_features)
                    for candle in candles[-120:]
                ],
            },
            "pivot_ladders": _pivot_ladders(
                candles, timeframe, features=active_features
            ),
        },
    }


def _zoom_candidate_score(bar_count: int) -> float:
    if 100 <= bar_count <= 140:
        return abs(bar_count - 120)
    if bar_count > 140:
        return 10.0 + 20.0 * math.log2(bar_count / 140.0)
    return float((100 - bar_count) * 2)


def _segment_rows(
    candles: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    include_endpoint_day: bool = False,
) -> list[dict[str, Any]]:
    effective_end = (
        end + timedelta(days=1) - timedelta(microseconds=1)
        if include_endpoint_day
        else end
    )
    return [
        candle
        for candle in candles
        if isinstance(candle.get("parsed_date"), datetime)
        and start <= candle["parsed_date"] <= effective_end
    ]


def _segment_includes_endpoint_day(segment: Mapping[str, Any]) -> bool:
    explicit = segment.get("end_inclusive_trading_day")
    if isinstance(explicit, bool):
        return explicit
    raw_end = str(segment.get("end") or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_end))


def _coverage_status(*, start_covered: bool, end_covered: bool, row_count: int) -> str:
    if not row_count:
        return "unavailable"
    if start_covered and end_covered:
        return "full"
    if not start_covered and not end_covered:
        return "partial_before_start_and_after_end"
    return "partial_before_start" if not start_covered else "partial_after_end"


def _segment_timeframe_coverage(
    datasets: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    include_endpoint_day: bool,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for dataset in datasets:
        rows = _segment_rows(
            dataset["candles"],
            start,
            end,
            include_endpoint_day=include_endpoint_day,
        )
        start_covered = dataset["first"].date() <= start.date()
        end_covered = dataset["last"].date() >= end.date()
        facts.append(
            {
                "timeframe": dataset["timeframe"],
                "coverage_status": _coverage_status(
                    start_covered=start_covered,
                    end_covered=end_covered,
                    row_count=len(rows),
                ),
                "start_covered": start_covered,
                "end_covered": end_covered,
                "dataset_first_timestamp": _compact_date(
                    {"parsed_date": dataset["first"]}
                ),
                "dataset_last_timestamp": _compact_date(
                    {"parsed_date": dataset["last"]}
                ),
                "segment_candle_count": len(rows),
                "endpoint_day_included": include_endpoint_day,
            }
        )
    return facts


def _segment_indicator_summary(
    candles: list[dict[str, Any]],
    *,
    features: Iterable[str] | None = None,
    rsi_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ewo_values = [float(row["ewo"]) for row in candles if row.get("ewo") is not None]
    volumes = [float(row["volume"]) for row in candles if row.get("volume") is not None]
    zero_crosses = sum(
        1
        for previous, current in zip(ewo_values, ewo_values[1:])
        if (previous < 0 <= current) or (previous > 0 >= current)
    )
    highest = max(candles, key=lambda row: float(row["high"]))
    lowest = min(candles, key=lambda row: float(row["low"]))
    technical_features = summarize_indicators(candles, features=features)
    if "rsi" in technical_features and rsi_source is not None:
        technical_features["rsi"]["source"] = dict(rsi_source)
    return {
        "highest": {"date": _compact_date(highest), "price": _rounded(highest["high"], 4)},
        "lowest": {"date": _compact_date(lowest), "price": _rounded(lowest["low"], 4)},
        "cumulative_volume": _rounded(sum(volumes), 0) if volumes else None,
        "average_volume": _rounded(sum(volumes) / len(volumes), 0) if volumes else None,
        "ewo_positive_peak": _rounded(max(ewo_values), 4) if ewo_values else None,
        "ewo_negative_peak": _rounded(min(ewo_values), 4) if ewo_values else None,
        "ewo_absolute_peak": (
            _rounded(max(abs(value) for value in ewo_values), 4)
            if ewo_values
            else None
        ),
        "ewo_zero_crosses": zero_crosses,
        "technical_features": technical_features,
    }


def build_zoom_windows(
    paths: Iterable[Path],
    segments: Iterable[Mapping[str, Any]],
    *,
    features: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Choose the closest complete 100-140 bar child view for each parent segment."""
    active_features = normalize_features(features)
    datasets: list[dict[str, Any]] = []
    for path in paths:
        resolved = Path(path).resolve()
        candles = enrich_candles(load_candles(resolved), features=active_features)
        parsed = [
            row["parsed_date"]
            for row in candles
            if isinstance(row.get("parsed_date"), datetime)
        ]
        if not parsed:
            continue
        datasets.append(
            {
                "path": resolved,
                "timeframe": infer_timeframe(resolved),
                "candles": candles,
                "first": min(parsed),
                "last": max(parsed),
                "metadata": load_market_metadata(resolved),
            }
        )

    windows: list[dict[str, Any]] = []
    for segment in segments:
        start = parse_date(segment.get("start"))
        end = parse_date(segment.get("end"))
        if start is None or end is None or end <= start:
            continue
        include_endpoint_day = _segment_includes_endpoint_day(segment)
        timeframe_coverage = _segment_timeframe_coverage(
            datasets,
            start,
            end,
            include_endpoint_day=include_endpoint_day,
        )
        candidates: list[tuple[float, int, dict[str, Any], list[dict[str, Any]]]] = []
        for dataset in datasets:
            if dataset["first"].date() > start.date() or dataset["last"].date() < end.date():
                continue
            rows = _segment_rows(
                dataset["candles"],
                start,
                end,
                include_endpoint_day=include_endpoint_day,
            )
            if len(rows) < 5:
                continue
            score = _zoom_candidate_score(len(rows))
            # Prefer a finer source when two candidates are equally close to 120 bars.
            candidates.append((score, -len(rows), dataset, rows))

        if not candidates:
            windows.append(
                {
                    "segment": dict(segment),
                    "status": "unavailable",
                    "reason": "No single supplied timeframe covers the complete segment.",
                    "timeframe_coverage": timeframe_coverage,
                }
            )
            continue

        _, _, dataset, rows = min(candidates, key=lambda item: (item[0], item[1]))
        if len(rows) > 140:
            view_mode = "consecutive_aggregation_to_120_bars"
            view_rows = _scaled_history_view(
                rows, 120, features=active_features
            )
        else:
            view_mode = "unchanged_native_bars"
            view_rows = [
                [
                    _compact_date(row),
                    _compact_date(row),
                    _rounded(row.get("open"), 4),
                    _rounded(row.get("high"), 4),
                    _rounded(row.get("low"), 4),
                    _rounded(row.get("close"), 4),
                    _rounded(row.get("volume"), 0),
                    *_feature_values(row, active_features),
                    1,
                ]
                for row in rows
            ]
        metadata = dataset["metadata"]
        windows.append(
            {
                "segment": dict(segment),
                "status": "prepared",
                "source": str(dataset["path"]),
                "source_timeframe": dataset["timeframe"],
                "timeframe_coverage": timeframe_coverage,
                "requested_symbol": metadata.get("requested_symbol"),
                "provider_symbol": metadata.get("provider_symbol"),
                "session": metadata.get("session", "unknown"),
                "view_mode": view_mode,
                "native_bar_count": len(rows),
                "view_bar_count": len(view_rows),
                "columns": [
                    "start",
                    "end",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    *_feature_columns(active_features),
                    "source_bars",
                ],
                "bars": view_rows,
                "segment_metrics": _segment_indicator_summary(
                    rows,
                    features=active_features,
                    rsi_source=build_indicator_source_metadata(
                        dataset["path"],
                        metadata,
                        timeframe=dataset["timeframe"],
                    ),
                ),
                "pivot_ladders": _pivot_ladders(
                    rows, dataset["timeframe"], features=active_features
                ),
            }
        )
    return windows


def _aggregate_by_date(candles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candle in candles:
        parsed = candle.get("parsed_date")
        if not isinstance(parsed, datetime):
            continue
        grouped.setdefault(parsed.date().isoformat(), []).append(candle)

    result: dict[str, dict[str, Any]] = {}
    for date, rows in grouped.items():
        rows.sort(key=lambda row: row["parsed_date"])
        volumes = [float(row["volume"]) for row in rows if row.get("volume") is not None]
        result[date] = {
            "open": rows[0].get("open"),
            "high": max(float(row["high"]) for row in rows),
            "low": min(float(row["low"]) for row in rows),
            "close": float(rows[-1]["close"]),
            "volume": sum(volumes) if volumes else None,
            "bars": len(rows),
        }
    return result


def _relative_error(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    denominator = max(abs(float(left)), abs(float(right)), 1e-12)
    return abs(float(left) - float(right)) / denominator


def reconcile_timeframes(
    paths: Iterable[Path],
    *,
    price_tolerance_pct: float = 0.25,
    volume_tolerance_pct: float = 25.0,
    max_overlap_days: int = 60,
) -> dict[str, Any]:
    """Reconcile price structure separately from cross-timeframe volume coverage."""
    resolved = [Path(path).resolve() for path in paths]
    daily_path = next((path for path in resolved if infer_timeframe(path) == "daily"), None)
    child_paths = [
        path for path in resolved if infer_timeframe(path) in INTRADAY_TIMEFRAMES
    ]
    if daily_path is None or not child_paths:
        return {
            "overall_status": "insufficient_data",
            "policy": "Daily and at least one intraday dataset are required.",
            "daily_source": str(daily_path) if daily_path else None,
            "comparisons": [],
        }

    daily = _aggregate_by_date(load_candles(daily_path))
    daily_metadata = load_market_metadata(daily_path)
    comparisons: list[dict[str, Any]] = []
    price_tolerance = price_tolerance_pct / 100.0
    volume_tolerance = volume_tolerance_pct / 100.0

    for child_path in child_paths:
        child = _aggregate_by_date(load_candles(child_path))
        child_metadata = load_market_metadata(child_path)
        overlap = sorted(set(daily) & set(child))[-max_overlap_days:]
        examples: list[dict[str, Any]] = []
        price_matches = 0
        volume_matches = 0
        volume_comparisons = 0
        for date in overlap:
            parent_row = daily[date]
            child_row = child[date]
            field_errors = {
                field: _relative_error(parent_row.get(field), child_row.get(field))
                for field in ("open", "high", "low", "close")
            }
            price_error = max(
                (error for error in field_errors.values() if error is not None),
                default=1.0,
            )
            volume_error = _relative_error(
                parent_row.get("volume"), child_row.get("volume")
            )
            if price_error <= price_tolerance:
                price_matches += 1
            if volume_error is not None:
                volume_comparisons += 1
                if volume_error <= volume_tolerance:
                    volume_matches += 1
            if price_error > price_tolerance or (
                volume_error is not None and volume_error > volume_tolerance
            ):
                examples.append(
                    {
                        "date": date,
                        "daily": parent_row,
                        "intraday_aggregate": child_row,
                        "maximum_price_error_pct": round(price_error * 100.0, 4),
                        "volume_error_pct": (
                            round(volume_error * 100.0, 4)
                            if volume_error is not None
                            else None
                        ),
                    }
                )

        overlap_count = len(overlap)
        price_match_rate = price_matches / overlap_count if overlap_count else 0.0
        volume_match_rate = (
            volume_matches / volume_comparisons if volume_comparisons else None
        )
        price_passed = overlap_count >= 3 and price_match_rate >= 0.90
        volume_passed = volume_match_rate is None or volume_match_rate >= 0.75
        status = (
            "passed"
            if price_passed and volume_passed
            else "passed_for_price_only"
            if price_passed
            else "failed"
        )
        examples.sort(
            key=lambda item: max(
                item["maximum_price_error_pct"], item["volume_error_pct"] or 0.0
            ),
            reverse=True,
        )
        comparisons.append(
            {
                "intraday_source": str(child_path),
                "timeframe": infer_timeframe(child_path),
                "status": status,
                "quarantined": not price_passed,
                "price_status": "passed" if price_passed else "failed",
                "price_quarantined": not price_passed,
                "volume_status": (
                    "passed"
                    if volume_passed
                    else "not_comparable_across_timeframes"
                ),
                "volume_quarantined": not volume_passed,
                "overlap_days": overlap_count,
                "price_match_rate": round(price_match_rate, 4),
                "volume_match_rate": (
                    round(volume_match_rate, 4)
                    if volume_match_rate is not None
                    else None
                ),
                "price_tolerance_pct": price_tolerance_pct,
                "volume_tolerance_pct": volume_tolerance_pct,
                "disputed_price_dates": [
                    item["date"]
                    for item in examples
                    if item["maximum_price_error_pct"] > price_tolerance_pct
                ][:10],
                "provenance": {
                    "daily": {
                        "requested_symbol": daily_metadata.get("requested_symbol"),
                        "provider_symbol": daily_metadata.get("provider_symbol"),
                        "exchange": daily_metadata.get("exchange"),
                        "session": daily_metadata.get("session"),
                    },
                    "intraday": {
                        "requested_symbol": child_metadata.get("requested_symbol"),
                        "provider_symbol": child_metadata.get("provider_symbol"),
                        "exchange": child_metadata.get("exchange"),
                        "session": child_metadata.get("session"),
                    },
                    "provider_substitution": bool(
                        child_metadata.get("requested_symbol")
                        and child_metadata.get("provider_symbol")
                        and child_metadata.get("requested_symbol")
                        != child_metadata.get("provider_symbol")
                    ),
                },
                "largest_mismatches": examples[:5],
            }
        )

    price_failed = any(item["price_status"] == "failed" for item in comparisons)
    volume_limited = any(item["volume_quarantined"] for item in comparisons)
    return {
        "overall_status": (
            "failed"
            if not comparisons or price_failed
            else "passed_with_volume_limitations"
            if volume_limited
            else "passed"
        ),
        "policy": (
            "Price and volume are reconciled independently. A price-failed intraday "
            "dataset cannot confirm parent boundaries. A price-passed dataset may support "
            "child structure, but quarantined volume may only be compared within its own "
            "unchanged feed and timeframe. Disputed dates cannot define cross-timeframe pivots."
        ),
        "daily_source": str(daily_path),
        "comparisons": comparisons,
    }


def discover_symbol_files(workspace: Path, symbol: str) -> list[Path]:
    ticker = symbol.split(":")[-1].lower().strip()
    if not ticker:
        return []
    paths = sorted(Path(workspace).glob(f"tv_{ticker}_*.json"))
    selected: dict[str, tuple[int, float, Path]] = {}
    for path in paths:
        try:
            bar_count = len(load_candles(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        timeframe = infer_timeframe(path)
        candidate = (bar_count, path.stat().st_mtime, path)
        if timeframe not in selected or candidate[:2] > selected[timeframe][:2]:
            selected[timeframe] = candidate
    return [
        item[2]
        for timeframe, item in sorted(
            selected.items(),
            key=lambda pair: (
                timeframe_sort_key(pair[0]) if pair[0] != "unknown" else (1, "unknown")
            ),
        )
    ]


def _is_completed_wave(row: Mapping[str, Any]) -> bool:
    status = str(row.get("wave_status", "completed")).lower()
    return status.startswith("completed") or status in {"locked", "confirmed"}


def _wave_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("wave_rows", "waves", "records", "11_ActiveDatabaseRecords"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def verify_wave_file(candle_path: Path, wave_path: Path) -> dict[str, Any]:
    """Aggregate volume/EWO by wave and run the existing verification rules."""
    from scripts.elliott_analysis_layers import (
        add_correction_classification,
        add_impulse_verification,
    )

    candles = add_ewo(load_candles(candle_path))
    wave_value = json.loads(Path(wave_path).read_text(encoding="utf-8", errors="replace"))
    rows = _wave_rows(wave_value)
    if not rows:
        raise ValueError(f"No wave rows found in {wave_path}.")

    for row in rows:
        row.setdefault("wave_ewo_peak", None)
        row.setdefault("wave_ewo_min", None)
        row.setdefault("wave_ewo_max", None)
        row.setdefault("ewo_zero_touch", None)
        row.setdefault("cumulative_volume", None)
        if not _is_completed_wave(row):
            continue
        start = parse_date(row.get("start_date"))
        end = parse_date(row.get("end_date"))
        if start is None or end is None:
            continue
        subset = [
            candle
            for candle in candles
            if candle["parsed_date"] is not None and start <= candle["parsed_date"] <= end
        ]
        ewo_values = [float(candle["ewo"]) for candle in subset if candle["ewo"] is not None]
        volumes = [float(candle["volume"]) for candle in subset if candle["volume"] is not None]
        if ewo_values:
            row["wave_ewo_min"] = min(ewo_values)
            row["wave_ewo_max"] = max(ewo_values)
            row["wave_ewo_peak"] = max(abs(row["wave_ewo_min"]), abs(row["wave_ewo_max"]))
            row["ewo_zero_touch"] = row["wave_ewo_min"] <= 0 <= row["wave_ewo_max"]
        if volumes:
            row["cumulative_volume"] = sum(volumes)

    verified, impulse_summary = add_impulse_verification(rows)
    classified = add_correction_classification(verified)
    return {
        "candle_source": str(Path(candle_path).resolve()),
        "wave_source": str(Path(wave_path).resolve()),
        "impulse_summary": impulse_summary,
        "waves": classified,
    }
