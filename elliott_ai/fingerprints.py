"""Deterministic, cutoff-safe, versioned Elliott wave fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from .correction_semantics import combination_variant, structure_family
from .evidence import EVIDENCE_CONTRACT_VERSION, make_evidence_item
from .indicators import enrich_candles, normalize_features, simple_moving_average
from .market_data import (
    infer_timeframe,
    load_candles,
    load_market_metadata,
    parse_date,
)


FEATURE_SCHEMA_VERSION = "wave-fingerprint-1.0.0"
CALCULATION_VERSION = "wave-fingerprint-calc-1.0.0"
ROLE_TEMPLATE_VERSION = "wave-role-template-1.0.0"
DEFAULT_VOLUME_BASELINE_PERIOD = 50

FEATURE_SCHEMA_DEFINITION: dict[str, Any] = {
    "schema_version": FEATURE_SCHEMA_VERSION,
    "calculation_version": CALCULATION_VERSION,
    "required_blocks": [
        "identity",
        "origin",
        "cutoff",
        "data_quality",
        "price",
        "structural",
        "indicators",
        "evidence_items",
    ],
    "optional_indicator_blocks": ["rsi", "volume", "ewo", "macd", "volatility"],
    "missing_value_policy": "Use null with unavailable or incomparable; never substitute zero.",
    "label_policy": "A fingerprint describes a supplied candidate and never relabels it.",
    "look_ahead_policy": "Filter source candles at cutoff before calculating any feature.",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def fingerprint_content_hash(fingerprint: Mapping[str, Any]) -> str:
    """Return the stable content hash, excluding the hash field itself."""
    payload = deepcopy(dict(fingerprint))
    payload.pop("content_hash", None)
    return _hash_payload(payload)


def _finalize_fingerprint(fingerprint: dict[str, Any]) -> dict[str, Any]:
    fingerprint["content_hash"] = fingerprint_content_hash(fingerprint)
    return fingerprint


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _date_only(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) == 10


def _on_or_after(actual: datetime, boundary: datetime, raw: Any) -> bool:
    return actual.date() >= boundary.date() if _date_only(raw) else actual >= boundary


def _on_or_before(actual: datetime, boundary: datetime, raw: Any) -> bool:
    return actual.date() <= boundary.date() if _date_only(raw) else actual <= boundary


def _is_after(left: datetime, right: datetime, right_raw: Any) -> bool:
    return left.date() > right.date() if _date_only(right_raw) else left > right


def _anchor(wave: Mapping[str, Any], side: str, field: str) -> Any:
    value = wave.get(side)
    return value.get(field) if isinstance(value, Mapping) else None


def _position(wave: Mapping[str, Any]) -> str:
    raw = wave.get("sequence_position") or wave.get("wave") or wave.get("label") or "unknown"
    return str(raw).strip().replace("(", "").replace(")", "") or "unknown"


def _detailed_family(value: Any, positions: Iterable[str] = ()) -> str:
    family = structure_family(value)
    if family != "combination":
        return family if family != "other" else "unknown"
    variant = combination_variant(value, positions)
    return "triple-three" if variant == "triple" else "double-three"


def _prepare_candles(candles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for index, raw in enumerate(candles):
        row = dict(raw)
        parsed = row.get("parsed_date")
        if not isinstance(parsed, datetime):
            parsed = parse_date(row.get("date") or row.get("time") or row.get("timestamp"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        if parsed is None or high is None or low is None or close is None:
            continue
        row.update(
            {
                "parsed_date": parsed,
                "date": str(row.get("date") or _iso(parsed)),
                "open": _number(row.get("open")),
                "high": high,
                "low": low,
                "close": close,
                "volume": _number(row.get("volume")),
                "source_position": row.get("source_position", index),
            }
        )
        prepared.append(row)
    prepared.sort(key=lambda row: row["parsed_date"])
    return prepared


def _source_identity(
    metadata: Mapping[str, Any] | None,
    *,
    symbol: str | None,
    timeframe: str,
    source_path: str | None,
    price_field: str,
) -> dict[str, Any]:
    source = dict(metadata or {})
    provider = source.get("provider") or source.get("source_provider") or source.get("source_mode") or "file_snapshot"
    venue = source.get("exchange") or source.get("listed_exchange") or "unknown"
    resolved_symbol = (
        symbol
        or source.get("resolved_symbol")
        or source.get("chart_symbol")
        or source.get("provider_symbol")
        or source.get("requested_symbol")
        or "unknown"
    )
    market_type = source.get("market_type") or source.get("instrument_type") or source.get("security_type") or "unknown"
    quote_currency = source.get("quote_currency") or source.get("currency") or source.get("quote") or "unknown"
    feed_identity = source.get("feed_identity") or "|".join(
        (
            str(provider),
            str(venue),
            str(resolved_symbol),
            str(market_type),
            timeframe,
            str(source_path or "memory"),
        )
    )
    return {
        "provider": str(provider),
        "venue": str(venue),
        "symbol": str(resolved_symbol),
        "market_type": str(market_type),
        "quote_currency": str(quote_currency),
        "timeframe": timeframe,
        "price_field": price_field,
        "feed_identity": str(feed_identity),
        "volume_scope": str(source.get("volume_scope") or "unknown"),
        "session": str(source.get("session") or "unknown"),
        "source_path": str(source_path) if source_path else None,
    }


def _slope(values: Iterable[Any]) -> float | None:
    points = [
        (float(index), float(value))
        for index, value in enumerate(values)
        if _number(value) is not None
    ]
    if len(points) < 2:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator == 0:
        return 0.0
    return sum(
        (point[0] - mean_x) * (point[1] - mean_y) for point in points
    ) / denominator


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [float(value) for value in values if _number(value) is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _median(values: Iterable[Any]) -> float | None:
    numbers = [float(value) for value in values if _number(value) is not None]
    return float(median(numbers)) if numbers else None


def _timing(values: list[float | None], *, absolute: bool = False, mode: str = "max") -> float | None:
    candidates = [
        (index, abs(float(value)) if absolute else float(value))
        for index, value in enumerate(values)
        if value is not None
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: item[1]) if mode == "max" else min(candidates, key=lambda item: item[1])
    return selected[0] / max(1, len(values) - 1)


def _zero_crossings(values: list[float | None]) -> int:
    valid = [float(value) for value in values if value is not None]
    crossings = 0
    for previous, current in zip(valid, valid[1:]):
        if (previous < 0 <= current) or (previous > 0 >= current):
            crossings += 1
    return crossings


def _ratio(value: float | None, reference: float | None) -> float | None:
    if value is None or reference in (None, 0):
        return None
    return abs(float(value)) / abs(float(reference))


def _ratio_feature(reference_kind: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "value": None,
        "reference_wave_id": None,
        "reference_kind": reference_kind,
        "reason": "No valid reference wave was supplied.",
    }


def _pivot_alignment(raw_date: Any, raw_price: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    expected = parse_date(raw_date)
    actual = row.get("parsed_date")
    price = _number(raw_price)
    if expected is None or not isinstance(actual, datetime) or price is None:
        return {"status": "unavailable", "date_aligned": None, "price_aligned": None}
    date_aligned = expected.date() == actual.date() if _date_only(raw_date) else expected == actual
    tolerance = max(1e-8, abs(price) * 1e-6)
    matched_fields = [
        field
        for field in ("high", "low", "close")
        if _number(row.get(field)) is not None
        and abs(float(row[field]) - price) <= tolerance
    ]
    return {
        "status": "aligned" if date_aligned and matched_fields else "not_aligned",
        "date_aligned": date_aligned,
        "price_aligned": bool(matched_fields),
        "matched_price_fields": matched_fields,
        "indicator_bar_timestamp": _iso(actual),
    }


def _boundary_price(
    wave: Mapping[str, Any], side: str, row: Mapping[str, Any], *, allow_anchor: bool
) -> tuple[float, dict[str, Any]]:
    alignment = _pivot_alignment(_anchor(wave, side, "date"), _anchor(wave, side, "price"), row)
    anchor_price = _number(_anchor(wave, side, "price"))
    if allow_anchor and alignment["status"] == "aligned" and anchor_price is not None:
        return anchor_price, alignment
    return float(row["close"]), alignment


def _pivot_count(rows: list[Mapping[str, Any]]) -> int:
    if len(rows) < 3:
        return 0
    count = 0
    for previous, current, following in zip(rows, rows[1:], rows[2:]):
        if float(current["high"]) > float(previous["high"]) and float(current["high"]) >= float(following["high"]):
            count += 1
        if float(current["low"]) < float(previous["low"]) and float(current["low"]) <= float(following["low"]):
            count += 1
    return count


def _linear_fit(values: list[float]) -> dict[str, float | None]:
    if len(values) < 2:
        return {"slope": None, "r_squared": None, "residual_std": None, "intercept": None}
    mean_x = (len(values) - 1) / 2.0
    mean_y = sum(values) / len(values)
    denominator = sum((index - mean_x) ** 2 for index in range(len(values)))
    slope = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values)) / denominator if denominator else 0.0
    intercept = mean_y - slope * mean_x
    residuals = [value - (intercept + slope * index) for index, value in enumerate(values)]
    residual_total = sum(value * value for value in residuals)
    total = sum((value - mean_y) ** 2 for value in values)
    return {
        "slope": slope,
        "intercept": intercept,
        "r_squared": 1.0 - residual_total / total if total else 1.0,
        "residual_std": math.sqrt(residual_total / len(values)),
    }


def _channel_features(closes: list[float]) -> dict[str, Any]:
    fit = _linear_fit(closes)
    if len(closes) < 5:
        return {
            "availability": "unavailable",
            "fit_r_squared": fit["r_squared"],
            "terminal_break_status": "unavailable",
            "terminal_deviation_sigma": None,
        }
    prior_fit = _linear_fit(closes[:-1])
    residual_std = prior_fit["residual_std"]
    predicted = float(prior_fit["intercept"]) + float(prior_fit["slope"]) * (len(closes) - 1)
    deviation = closes[-1] - predicted
    sigma = deviation / float(residual_std) if residual_std not in (None, 0) else 0.0
    status = "upper_break" if sigma > 2.0 else "lower_break" if sigma < -2.0 else "inside_channel"
    return {
        "availability": "available",
        "fit_r_squared": fit["r_squared"],
        "slope_per_bar": fit["slope"],
        "terminal_break_status": status,
        "terminal_deviation_sigma": sigma,
        "method": "OLS close channel; terminal bar tested against prior-bar fit.",
    }


def _acceleration_features(closes: list[float]) -> dict[str, Any]:
    if len(closes) < 6:
        return {"availability": "unavailable", "reason": "At least six candles are required."}
    midpoint = len(closes) // 2
    first = _slope(closes[: midpoint + 1])
    second = _slope(closes[midpoint:])
    ratio = _ratio(second, first)
    state = "unavailable"
    if first is not None and second is not None:
        state = "accelerating" if abs(second) > abs(first) * 1.05 else "decelerating" if abs(second) < abs(first) * 0.95 else "stable"
    return {
        "availability": "available",
        "first_half_slope": first,
        "second_half_slope": second,
        "absolute_slope_ratio": ratio,
        "state": state,
    }


def _series_block(values: list[float | None]) -> dict[str, Any]:
    valid = [float(value) for value in values if value is not None]
    return {
        "availability": "available" if valid else "unavailable",
        "start_value": values[0] if values else None,
        "end_value": values[-1] if values else None,
        "minimum": min(valid) if valid else None,
        "maximum": max(valid) if valid else None,
        "mean": sum(valid) / len(valid) if valid else None,
        "median": float(median(valid)) if valid else None,
        "absolute_peak": max((abs(value) for value in valid), default=None),
        "slope_per_bar": _slope(values),
        "zero_line_crossings": _zero_crossings(values),
        "absolute_peak_timing": _timing(values, absolute=True),
        "valid_observations": len(valid),
        "comparison_identity": None,
        "comparability": "unavailable",
        "aligned_divergence_candidate": None,
    }


def _contraction_state(values: list[float | None]) -> str:
    valid = [abs(float(value)) for value in values if value is not None]
    if len(valid) < 4:
        return "unavailable"
    midpoint = len(valid) // 2
    first = sum(valid[:midpoint]) / midpoint
    second = sum(valid[midpoint:]) / (len(valid) - midpoint)
    return "expanding" if second > first * 1.05 else "contracting" if second < first * 0.95 else "stable"


def _rsi_block(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    values = [_number(row.get("rsi")) for row in rows]
    valid = [float(value) for value in values if value is not None]
    statuses = [str(row.get("rsi_status") or "unknown") for row in rows]
    block = _series_block(values)
    block.update(
        {
            "proportion_above_50": sum(value > 50 for value in valid) / len(valid) if valid else None,
            "proportion_below_50": sum(value < 50 for value in valid) / len(valid) if valid else None,
            "minimum_timing": _timing(values, mode="min"),
            "maximum_timing": _timing(values, mode="max"),
            "warm_up_status": "ready" if valid else "warming_up",
            "warming_up_observations": statuses.count("warming_up"),
            "gap_observations": statuses.count("invalid_input"),
            "gap_status": "gaps_present" if "invalid_input" in statuses else "continuous",
            "calculation_method": rows[-1].get("rsi_period") if rows else None,
        }
    )
    return block


def _volume_block(
    rows: list[Mapping[str, Any]],
    baseline_values: list[float | None],
    source: Mapping[str, Any],
    price_change_pct: float | None,
    *,
    baseline_period: int,
    source_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    volumes = [_number(row.get("volume")) for row in rows]
    valid = [float(value) for value in volumes if value is not None]
    ratios = [
        float(volume) / float(baseline)
        if volume is not None and baseline not in (None, 0)
        else None
        for volume, baseline in zip(volumes, baseline_values)
    ]
    valid_ratios = [float(value) for value in ratios if value is not None]
    quarantine = bool(source_metadata.get("volume_quarantined")) or str(
        source_metadata.get("volume_status") or ""
    ).lower() == "quarantined"
    unavailable_reason = None
    if quarantine:
        unavailable_reason = str(source_metadata.get("volume_quarantine_reason") or "Volume feed is quarantined.")
    elif not valid:
        unavailable_reason = "No valid volume observations exist inside the wave."
    terminal_ratio = (
        float(volumes[-1]) / float(median(valid))
        if valid and volumes[-1] is not None and median(valid) != 0
        else None
    )
    peak_location = None
    if valid:
        peak_index = max(
            (index for index, value in enumerate(volumes) if value is not None),
            key=lambda index: float(volumes[index]),
        )
        peak_location = peak_index / max(1, len(rows) - 1)
    mean_ratio = sum(valid_ratios) / len(valid_ratios) if valid_ratios else None
    effort_result = (
        mean_ratio / abs(float(price_change_pct))
        if mean_ratio is not None and price_change_pct not in (None, 0)
        else None
    )
    return {
        "availability": "unavailable" if unavailable_reason else "available",
        "total_raw": sum(valid) if valid else None,
        "average_raw": sum(valid) / len(valid) if valid else None,
        "median_raw": float(median(valid)) if valid else None,
        "maximum_raw": max(valid) if valid else None,
        "start_raw": volumes[0] if volumes else None,
        "end_raw": volumes[-1] if volumes else None,
        "rolling_baseline_period": baseline_period,
        "average_ratio_to_baseline": mean_ratio,
        "median_ratio_to_baseline": float(median(valid_ratios)) if valid_ratios else None,
        "volume_slope_per_bar": _slope(volumes),
        "terminal_volume_ratio_to_wave_median": terminal_ratio,
        "peak_volume_location": peak_location,
        "effort_vs_result": effort_result,
        "effort_vs_result_method": "Mean normalized volume divided by absolute price return percentage.",
        "proportion_above_baseline": sum(value > 1.0 for value in valid_ratios) / len(valid_ratios) if valid_ratios else None,
        "feed_comparability": "self",
        "volume_quarantine_status": "quarantined" if quarantine else "clear",
        "unavailable_reason": unavailable_reason,
        "source": {
            field: source.get(field)
            for field in ("provider", "venue", "market_type", "symbol", "timeframe", "feed_identity", "volume_scope")
        },
    }


def _volatility_block(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    atr = [_number(row.get("atr_14")) for row in rows]
    atr_pct = [_number(row.get("atr_pct")) for row in rows]
    valid = [float(value) for value in atr if value is not None]
    start = atr[0] if atr else None
    end = atr[-1] if atr else None
    expansion = _ratio(end, start)
    if expansion is None:
        state = "unavailable"
    elif expansion > 1.05:
        state = "expanding"
    elif expansion < 0.95:
        state = "contracting"
    else:
        state = "stable"
    bb = [_number(row.get("bollinger_position")) for row in rows]
    kc = [_number(row.get("keltner_position")) for row in rows]
    close_mean = _mean(row.get("close") for row in rows)
    price_range = max(float(row["high"]) for row in rows) - min(float(row["low"]) for row in rows)
    terminal_ratio = end / (sum(valid) / len(valid)) if end is not None and valid and sum(valid) != 0 else None
    return {
        "availability": "available" if valid else "unavailable",
        "atr_start": start,
        "atr_end": end,
        "atr_mean": sum(valid) / len(valid) if valid else None,
        "atr_expansion_ratio": expansion,
        "atr_pct_mean": _mean(atr_pct),
        "normalized_range_pct": price_range / close_mean * 100.0 if close_mean else None,
        "volatility_state": state,
        "bollinger_outer_pierces": sum(value is not None and (value < 0 or value > 1) for value in bb),
        "keltner_outer_pierces": sum(value is not None and (value < 0 or value > 1) for value in kc),
        "bollinger_terminal_position": bb[-1] if bb else None,
        "keltner_terminal_position": kc[-1] if kc else None,
        "terminal_atr_ratio_to_wave_mean": terminal_ratio,
        "comparability": "self",
    }


def _indicator_availability_evidence(
    name: str, block: Mapping[str, Any], wave_id: str, source: Mapping[str, Any]
) -> dict[str, Any]:
    available = block.get("availability") == "available"
    return make_evidence_item(
        evidence_type=name,
        feature_name=f"{name}.availability",
        observed_value=block.get("availability"),
        status="neutral" if available else "unavailable",
        reason=(
            f"{name.upper()} observations are available for the cutoff-safe wave window."
            if available
            else f"{name.upper()} observations are unavailable for the cutoff-safe wave window."
        ),
        source_wave_or_pivot=wave_id,
        hard_rule=False,
        data_quality_status="available" if available else "insufficient_data",
        provenance=source,
        calculation_version=CALCULATION_VERSION,
    )


def generate_wave_fingerprint(
    wave: Mapping[str, Any],
    candles: Iterable[Mapping[str, Any]],
    *,
    source_metadata: Mapping[str, Any] | None = None,
    cutoff: Any,
    features: Iterable[str] | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    source_path: str | None = None,
    price_field: str = "pivot",
    origin_run_id: int | None = None,
    origin_resolution_id: int | None = None,
    volume_baseline_period: int = DEFAULT_VOLUME_BASELINE_PERIOD,
) -> dict[str, Any]:
    """Create one immutable fingerprint using no candle later than ``cutoff``."""
    cutoff_date = parse_date(cutoff)
    start_date = parse_date(_anchor(wave, "start", "date"))
    declared_end = parse_date(_anchor(wave, "end", "date"))
    if cutoff_date is None:
        raise ValueError("A valid fingerprint cutoff is required.")
    if start_date is None:
        raise ValueError("A valid wave start timestamp is required.")
    prepared = _prepare_candles(candles)
    cutoff_rows = [
        row
        for row in prepared
        if _on_or_before(row["parsed_date"], cutoff_date, cutoff)
    ]
    if not cutoff_rows:
        raise ValueError("No valid candles are available at or before the cutoff.")
    effective_end = declared_end or cutoff_date
    truncated = declared_end is not None and _is_after(declared_end, cutoff_date, cutoff)
    if truncated:
        effective_end = cutoff_date
    selected_indices = [
        index
        for index, row in enumerate(cutoff_rows)
        if _on_or_after(row["parsed_date"], start_date, _anchor(wave, "start", "date"))
        and _on_or_before(
            row["parsed_date"],
            effective_end,
            cutoff if truncated or declared_end is None else _anchor(wave, "end", "date"),
        )
    ]
    if not selected_indices:
        raise ValueError("No cutoff-safe candles fall within the supplied wave boundaries.")

    active_features = normalize_features(features)
    enriched = enrich_candles(cutoff_rows, features=active_features)
    start_index = selected_indices[0]
    end_index = selected_indices[-1]
    rows = enriched[start_index : end_index + 1]
    raw_rows = cutoff_rows[start_index : end_index + 1]
    start_price, start_alignment = _boundary_price(wave, "start", rows[0], allow_anchor=True)
    end_price, end_alignment = _boundary_price(wave, "end", rows[-1], allow_anchor=not truncated)
    if truncated:
        end_alignment = {
            "status": "cutoff_truncated",
            "date_aligned": None,
            "price_aligned": None,
            "indicator_bar_timestamp": _iso(rows[-1]["parsed_date"]),
        }

    resolved_timeframe = timeframe or str(wave.get("timeframe") or "unknown")
    source = _source_identity(
        source_metadata,
        symbol=symbol,
        timeframe=resolved_timeframe,
        source_path=source_path,
        price_field=price_field,
    )
    wave_id = str(wave.get("wave_id") or wave.get("id") or "unknown")
    label = _position(wave)
    parent_family = _detailed_family(
        wave.get("parent_pattern_family") or wave.get("parent_structure") or "unknown"
    )
    closes = [float(row["close"]) for row in rows]
    path = [start_price, *closes[1:-1], end_price] if len(closes) > 1 else [start_price, end_price]
    path_distance = sum(abs(current - previous) for previous, current in zip(path, path[1:]))
    signed_change = end_price - start_price
    percentage_change = signed_change / start_price * 100.0 if start_price else None
    highest = max(float(row["high"]) for row in rows)
    lowest = min(float(row["low"]) for row in rows)
    clock_seconds = (rows[-1]["parsed_date"] - rows[0]["parsed_date"]).total_seconds()
    price = {
        "start_price": start_price,
        "end_price": end_price,
        "signed_change": signed_change,
        "absolute_change": abs(signed_change),
        "percentage_change": percentage_change,
        "log_return": math.log(end_price / start_price) if start_price > 0 and end_price > 0 else None,
        "direction": "up" if signed_change > 0 else "down" if signed_change < 0 else "flat",
        "highest_high": highest,
        "lowest_low": lowest,
        "price_range": highest - lowest,
        "normalized_range_pct": (highest - lowest) / abs(start_price) * 100.0 if start_price else None,
        "directional_efficiency": abs(signed_change) / path_distance if path_distance else 0.0,
        "start_pivot_alignment": start_alignment,
        "end_pivot_alignment": end_alignment,
        "channel": _channel_features(closes),
        "acceleration": _acceleration_features(closes),
    }

    flags = {
        "price": True,
        "structural": True,
        "rsi": "rsi" in active_features,
        "volume": "volume" in active_features,
        "ewo": "ewo" in active_features,
        "macd": "macd" in active_features,
        "volatility": "volatility" in active_features,
        "scale": "scale" in active_features,
    }
    missing_volume = sum(row.get("volume") is None for row in raw_rows)
    boundary_problem = start_alignment["status"] != "aligned" or end_alignment["status"] not in {"aligned", "cutoff_truncated"}
    quality_status = "insufficient" if len(rows) < 2 else "partial" if boundary_problem or missing_volume else "sufficient"
    identity = {
        "symbol": source["symbol"],
        "provider": source["provider"],
        "venue": source["venue"],
        "market_type": source["market_type"],
        "quote_currency": source["quote_currency"],
        "timeframe": source["timeframe"],
        "price_field": source["price_field"],
        "wave_id": wave_id,
        "wave_label": label,
        "wave_degree": str(wave.get("degree") or "unknown"),
        "parent_wave_id": wave.get("parent_wave_id"),
        "parent_pattern_family": parent_family,
        "start_timestamp": _iso(rows[0]["parsed_date"]),
        "end_timestamp": _iso(rows[-1]["parsed_date"]),
        "start_index": start_index,
        "end_index": end_index,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "active_feature_flags": flags,
        "data_quality_status": quality_status,
    }
    identity_hash = _hash_payload(
        {
            "identity": identity,
            "cutoff_timestamp": _iso(cutoff_date),
            "origin_run_id": origin_run_id,
            "origin_resolution_id": origin_resolution_id,
        }
    )
    structural = {
        "duration_candles": len(rows),
        "duration_clock_seconds": clock_seconds,
        "duration_clock_days": clock_seconds / 86_400.0,
        "retracement_ratio": _ratio_feature("retracement"),
        "extension_ratio": _ratio_feature("extension"),
        "overlap_ratio": _ratio_feature("price_range_overlap"),
        "pivot_count": _pivot_count(rows),
        "internal_child_count": len(wave.get("child_wave_ids", [])) if isinstance(wave.get("child_wave_ids"), list) else None,
        "internal_structure_family": _detailed_family(wave.get("structure")),
        "child_structure_status": wave.get("child_structure_status"),
        "completion_status_at_cutoff": "provisional" if truncated else wave.get("completion_status", "unknown"),
        "data_sufficiency": quality_status,
    }

    indicators: dict[str, Any] = {}
    evidence_items = [
        make_evidence_item(
            evidence_type="structural",
            feature_name="wave.data_sufficiency",
            observed_value=quality_status,
            status="neutral" if quality_status == "sufficient" else "unavailable",
            reason="Fingerprint data sufficiency was measured at the declared cutoff.",
            source_wave_or_pivot=wave_id,
            hard_rule=False,
            data_quality_status=quality_status,
            provenance=source,
            calculation_version=CALCULATION_VERSION,
        )
    ]
    channel = price["channel"]
    evidence_items.append(
        make_evidence_item(
            evidence_type="channel",
            feature_name="price.channel.fit_r_squared",
            observed_value=channel.get("fit_r_squared"),
            comparison_or_expected_condition=channel.get("terminal_break_status"),
            status="neutral" if channel.get("availability") == "available" else "unavailable",
            reason="The within-wave close channel was measured at the declared cutoff.",
            source_wave_or_pivot=wave_id,
            hard_rule=False,
            data_quality_status=str(channel.get("availability")),
            provenance=source,
            calculation_version=CALCULATION_VERSION,
        )
    )
    if flags["volume"]:
        all_volumes = [_number(row.get("volume")) for row in enriched]
        baseline = simple_moving_average(all_volumes, volume_baseline_period)
        block = _volume_block(
            rows,
            baseline[start_index : end_index + 1],
            source,
            percentage_change,
            baseline_period=volume_baseline_period,
            source_metadata=dict(source_metadata or {}),
        )
        indicators["volume"] = block
        evidence_items.append(_indicator_availability_evidence("volume", block, wave_id, source))
    if flags["ewo"]:
        block = _series_block([_number(row.get("ewo")) for row in rows])
        indicators["ewo"] = block
        evidence_items.append(_indicator_availability_evidence("ewo", block, wave_id, source))
    if flags["macd"]:
        block = _series_block([_number(row.get("macd")) for row in rows])
        histogram = [_number(row.get("macd_histogram")) for row in rows]
        block.update(
            {
                "histogram_start": histogram[0] if histogram else None,
                "histogram_end": histogram[-1] if histogram else None,
                "histogram_absolute_peak": max((abs(float(value)) for value in histogram if value is not None), default=None),
                "histogram_state": _contraction_state(histogram),
                "histogram_slope_per_bar": _slope(histogram),
            }
        )
        indicators["macd"] = block
        evidence_items.append(_indicator_availability_evidence("macd", block, wave_id, source))
    if flags["rsi"]:
        block = _rsi_block(rows)
        indicators["rsi"] = block
        evidence_items.append(_indicator_availability_evidence("rsi", block, wave_id, source))
    if flags["volatility"]:
        block = _volatility_block(rows)
        indicators["volatility"] = block
        evidence_items.append(_indicator_availability_evidence("atr", block, wave_id, source))

    fingerprint: dict[str, Any] = {
        "status": "calculated",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "identity_hash": identity_hash,
        "identity": identity,
        "origin": {
            "analysis_run_id": origin_run_id,
            "degree_resolution_id": origin_resolution_id,
        },
        "cutoff": {
            "timestamp": _iso(cutoff_date),
            "last_source_candle_timestamp": _iso(cutoff_rows[-1]["parsed_date"]),
            "source_candles_available": len(cutoff_rows),
            "wave_end_truncated": truncated,
            "provisional": truncated,
            "look_ahead_policy": "Candles were filtered at cutoff before indicators and wave metrics were calculated.",
        },
        "source": source,
        "data_quality": {
            "status": quality_status,
            "wave_candles": len(rows),
            "missing_volume_observations": missing_volume,
            "start_boundary_alignment": start_alignment["status"],
            "end_boundary_alignment": end_alignment["status"],
        },
        "price": price,
        "structural": structural,
        "indicators": indicators,
        "evidence_items": evidence_items,
        "comparison_summary": None,
        "role_template": None,
        "policy": "Observed behavior only; this fingerprint cannot assign or relabel an Elliott wave.",
    }
    fingerprint["role_template"] = evaluate_role_template(fingerprint)
    return _finalize_fingerprint(fingerprint)


def _field(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _same_source(left: Mapping[str, Any], right: Mapping[str, Any], *, volume: bool = False) -> tuple[bool, list[str]]:
    fields = ["provider", "venue", "market_type", "symbol", "timeframe", "feed_identity"]
    if volume:
        fields.extend(("volume_scope", "quote_currency"))
    reasons = []
    for field in fields:
        left_value = _field(left, f"source.{field}") or _field(left, f"identity.{field}")
        right_value = _field(right, f"source.{field}") or _field(right, f"identity.{field}")
        if left_value != right_value:
            reasons.append(f"{field} differs ({left_value!r} versus {right_value!r})")
    return not reasons, reasons


def _structurally_comparable(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    for field in ("symbol", "timeframe", "wave_degree", "parent_wave_id", "parent_pattern_family"):
        left_value = _field(left, f"identity.{field}")
        right_value = _field(right, f"identity.{field}")
        if left_value != right_value:
            reasons.append(f"identity.{field} differs ({left_value!r} versus {right_value!r})")
    return not reasons, reasons


def _divergence(
    current: Mapping[str, Any], reference: Mapping[str, Any], indicator: str
) -> dict[str, Any]:
    current_alignment = _field(current, "price.end_pivot_alignment.status")
    reference_alignment = _field(reference, "price.end_pivot_alignment.status")
    if current_alignment != "aligned" or reference_alignment != "aligned":
        return {
            "comparability": "incomparable",
            "candidate": None,
            "reason": "Both indicator observations must align with their declared price pivots.",
        }
    current_price = _number(_field(current, "price.end_price"))
    reference_price = _number(_field(reference, "price.end_price"))
    current_value = _number(_field(current, f"indicators.{indicator}.end_value"))
    reference_value = _number(_field(reference, f"indicators.{indicator}.end_value"))
    if None in (current_price, reference_price, current_value, reference_value):
        return {
            "comparability": "unavailable",
            "candidate": None,
            "reason": "A price or aligned indicator endpoint is unavailable.",
        }
    if current_price < reference_price and current_value > reference_value:
        candidate = "bullish"
    elif current_price > reference_price and current_value < reference_value:
        candidate = "bearish"
    else:
        candidate = "none"
    return {
        "comparability": "comparable",
        "candidate": candidate,
        "price_difference_pct": (current_price / reference_price - 1.0) * 100.0 if reference_price else None,
        "indicator_difference": current_value - reference_value,
        "reason": "Aligned same-feed endpoints were compared without assuming a wave-letter outcome.",
    }


def compare_wave_fingerprints(
    current: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    relationship: str,
) -> dict[str, Any]:
    """Compare structurally paired fingerprints without selecting an Elliott label."""
    current_id = _field(current, "identity.wave_id")
    reference_id = _field(reference, "identity.wave_id")
    structurally_comparable, structural_reasons = _structurally_comparable(current, reference)
    source_comparable, source_reasons = _same_source(current, reference)
    comparable_fields: list[dict[str, Any]] = []
    incomparable_fields: list[dict[str, Any]] = []
    unavailable_reasons: list[str] = []
    ratios: dict[str, float | None] = {}
    evidence_items: list[dict[str, Any]] = []

    def compare_numeric(name: str, current_path: str, reference_path: str | None = None) -> None:
        reference_path = reference_path or current_path
        current_value = _number(_field(current, current_path))
        reference_value = _number(_field(reference, reference_path))
        ratio = _ratio(current_value, reference_value)
        if current_value is None or reference_value is None or ratio is None:
            reason = f"{name} is unavailable or has no valid non-zero reference."
            unavailable_reasons.append(reason)
            incomparable_fields.append({"field": name, "reason": reason})
            ratios[name] = None
            return
        ratios[name] = ratio
        comparable_fields.append(
            {
                "field": name,
                "current": current_value,
                "reference": reference_value,
                "normalized_ratio": ratio,
                "directional_difference": current_value - reference_value,
            }
        )
        evidence_items.append(
            make_evidence_item(
                evidence_type="indicator" if name.startswith(("ewo", "macd", "rsi")) else "structural",
                feature_name=name,
                observed_value=current_value,
                comparison_or_expected_condition={"reference_value": reference_value, "normalized_ratio": ratio},
                status="neutral",
                reason=f"{name} was compared deterministically; no label decision was made.",
                source_wave_or_pivot=current_id,
                comparison_wave_or_pivot=reference_id,
                hard_rule=False,
                data_quality_status="available",
                provenance=current.get("source", {}),
                calculation_version=CALCULATION_VERSION,
            )
        )

    if not structurally_comparable:
        unavailable_reasons.extend(structural_reasons)
    else:
        compare_numeric("price_absolute_change", "price.absolute_change")
        compare_numeric("duration_candles", "structural.duration_candles")
        compare_numeric("directional_efficiency", "price.directional_efficiency")
        compare_numeric("price_range", "price.price_range")

    indicator_fields = {
        "ewo_absolute_peak": "indicators.ewo.absolute_peak",
        "macd_absolute_peak": "indicators.macd.absolute_peak",
        "macd_histogram_absolute_peak": "indicators.macd.histogram_absolute_peak",
        "rsi_end": "indicators.rsi.end_value",
        "atr_mean": "indicators.volatility.atr_mean",
    }
    if structurally_comparable and source_comparable:
        for name, path in indicator_fields.items():
            if _field(current, path.rsplit(".", 1)[0]) is not None and _field(reference, path.rsplit(".", 1)[0]) is not None:
                compare_numeric(name, path)
    else:
        for name in indicator_fields:
            incomparable_fields.append({"field": name, "reason": "; ".join(source_reasons or structural_reasons)})

    volume_comparable, volume_reasons = _same_source(current, reference, volume=True)
    current_volume = _field(current, "indicators.volume")
    reference_volume = _field(reference, "indicators.volume")
    volume_clear = (
        isinstance(current_volume, Mapping)
        and isinstance(reference_volume, Mapping)
        and current_volume.get("volume_quarantine_status") == "clear"
        and reference_volume.get("volume_quarantine_status") == "clear"
    )
    if structurally_comparable and volume_comparable and volume_clear:
        compare_numeric("volume_total", "indicators.volume.total_raw")
        compare_numeric("volume_normalized", "indicators.volume.average_ratio_to_baseline")
    elif current_volume is not None or reference_volume is not None:
        reasons = volume_reasons or ["One or both volume feeds are unavailable or quarantined."]
        for name in ("volume_total", "volume_normalized"):
            incomparable_fields.append({"field": name, "reason": "; ".join(reasons)})
        evidence_items.append(
            make_evidence_item(
                evidence_type="volume",
                feature_name="volume.feed_comparability",
                observed_value="incomparable",
                comparison_or_expected_condition=reasons,
                status="incomparable",
                reason="Volume was not compared across incompatible or quarantined feeds.",
                source_wave_or_pivot=current_id,
                comparison_wave_or_pivot=reference_id,
                hard_rule=False,
                data_quality_status="incomparable",
                provenance=current.get("source", {}),
                calculation_version=CALCULATION_VERSION,
            )
        )

    divergence_candidates: dict[str, Any] = {}
    for indicator in ("rsi", "ewo", "macd"):
        if _field(current, f"indicators.{indicator}") is None or _field(reference, f"indicators.{indicator}") is None:
            continue
        divergence = _divergence(current, reference, indicator)
        divergence_candidates[indicator] = divergence
        evidence_items.append(
            make_evidence_item(
                evidence_type=indicator,
                feature_name=f"{indicator}.aligned_divergence",
                observed_value=divergence.get("candidate"),
                comparison_or_expected_condition="Aligned comparable price pivots",
                status="neutral" if divergence.get("comparability") == "comparable" else str(divergence.get("comparability")),
                reason=str(divergence.get("reason")),
                source_wave_or_pivot=current_id,
                comparison_wave_or_pivot=reference_id,
                hard_rule=False,
                data_quality_status=str(divergence.get("comparability")),
                provenance=current.get("source", {}),
                calculation_version=CALCULATION_VERSION,
            )
        )

    comparison = {
        "status": "comparable" if structurally_comparable else "incomparable",
        "relationship": str(relationship),
        "current_wave_id": current_id,
        "reference_wave_id": reference_id,
        "current_identity_hash": current.get("identity_hash"),
        "reference_identity_hash": reference.get("identity_hash"),
        "comparable_fields": comparable_fields,
        "incomparable_fields": incomparable_fields,
        "directional_differences": {
            "current_direction": _field(current, "price.direction"),
            "reference_direction": _field(reference, "price.direction"),
            "same_direction": _field(current, "price.direction") == _field(reference, "price.direction"),
        },
        "normalized_ratios": ratios,
        "divergence_candidates": divergence_candidates,
        "evidence_items": evidence_items,
        "unavailable_reasons": unavailable_reasons,
        "calculation_version": CALCULATION_VERSION,
        "policy": "No overall probability or Elliott label is produced.",
    }
    comparison["comparison_hash"] = _hash_payload(comparison)
    return comparison


def _template(
    role_id: str,
    description: str,
    *,
    efficiency_support: str,
    comparison_metric: str | None = None,
) -> dict[str, Any]:
    tendencies = [
        {
            "tendency_id": "directional_efficiency_context",
            "feature_path": "price.directional_efficiency",
            "operator": "gte" if efficiency_support == "high" else "lte",
            "threshold": 0.5 if efficiency_support == "high" else 0.65,
            "classification_when_met": "supportive_tendency",
            "classification_when_not_met": "contextual_observation",
            "description": "Directional efficiency is a tendency, never a structural requirement.",
        },
        {
            "tendency_id": "extreme_efficiency_countercontext",
            "feature_path": "price.directional_efficiency",
            "operator": "lte" if efficiency_support == "high" else "gte",
            "threshold": 0.2 if efficiency_support == "high" else 0.9,
            "classification_when_met": "contradictory_tendency",
            "classification_when_not_met": "contextual_observation",
            "description": "An extreme opposite efficiency profile is contradictory context, not invalidation.",
        },
    ]
    if comparison_metric:
        tendencies.append(
            {
                "tendency_id": "relative_momentum_context",
                "feature_path": f"comparison_summary.normalized_ratios.{comparison_metric}",
                "operator": "lte",
                "threshold": 1.0,
                "classification_when_met": "supportive_tendency",
                "classification_when_not_met": "contextual_observation",
                "description": "Relative momentum may support a role but is never mandatory.",
            }
        )
    return {
        "template_version": ROLE_TEMPLATE_VERSION,
        "role_id": role_id,
        "description": description,
        "hard_rules": [],
        "tendencies": tendencies,
    }


ROLE_TEMPLATES: dict[str, dict[str, Any]] = {
    "zigzag:A": _template("zigzag_a", "Wave A candidate inside a zigzag.", efficiency_support="high"),
    "zigzag:B": _template("zigzag_b", "Wave B candidate inside a zigzag.", efficiency_support="low"),
    "zigzag:C": _template("zigzag_c", "Wave C candidate inside a zigzag.", efficiency_support="high", comparison_metric="ewo_absolute_peak"),
    "flat:A": _template("flat_a", "Wave A candidate inside a flat.", efficiency_support="low"),
    "flat:B": _template("flat_b", "Wave B candidate inside a flat.", efficiency_support="low"),
    "flat:C": _template("flat_c", "Wave C candidate inside a flat.", efficiency_support="high", comparison_metric="ewo_absolute_peak"),
    "double-three:W": _template("double_three_w", "Wave W candidate inside a combination.", efficiency_support="low"),
    "combination:X": _template("connector_x", "First corrective connector X.", efficiency_support="low"),
    "double-three:Y": _template("double_three_y", "Wave Y candidate compared with W.", efficiency_support="low", comparison_metric="ewo_absolute_peak"),
    "combination:X2": _template("connector_x2", "Distinct second corrective connector X2.", efficiency_support="low"),
    "triple-three:Z": _template("triple_three_z", "Terminal Wave Z candidate compared with Y.", efficiency_support="low", comparison_metric="ewo_absolute_peak"),
    "impulse:1": _template("impulse_wave_1", "Impulse Wave 1 candidate.", efficiency_support="high"),
    "corrective:A": _template("corrective_wave_a", "Generic corrective Wave A candidate.", efficiency_support="low"),
}


def role_template_for(fingerprint: Mapping[str, Any]) -> dict[str, Any] | None:
    family = str(_field(fingerprint, "identity.parent_pattern_family") or "unknown")
    label = str(_field(fingerprint, "identity.wave_label") or "unknown")
    key = f"{family}:{label}"
    if key in ROLE_TEMPLATES:
        return deepcopy(ROLE_TEMPLATES[key])
    if label in {"X", "X2"}:
        return deepcopy(ROLE_TEMPLATES[f"combination:{label}"])
    if family == "triple-three" and label in {"W", "Y"}:
        return deepcopy(ROLE_TEMPLATES[f"double-three:{label}"])
    if label == "A":
        return deepcopy(ROLE_TEMPLATES["corrective:A"])
    return None


def _condition(value: float, operator: str, threshold: float) -> bool:
    if operator == "gte":
        return value >= threshold
    if operator == "lte":
        return value <= threshold
    raise ValueError(f"Unsupported template operator: {operator}")


def evaluate_role_template(fingerprint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Evaluate soft role tendencies without producing a label or probability."""
    template = role_template_for(fingerprint)
    if template is None:
        return None
    evaluations = []
    for tendency in template["tendencies"]:
        observed = _number(_field(fingerprint, tendency["feature_path"]))
        if observed is None:
            classification = "unavailable"
            status = "unavailable"
            reason = "The tendency could not be evaluated because its feature or comparison is unavailable."
        else:
            met = _condition(observed, tendency["operator"], float(tendency["threshold"]))
            classification = tendency["classification_when_met"] if met else tendency["classification_when_not_met"]
            status = "supportive" if classification == "supportive_tendency" else "contradictory" if classification == "contradictory_tendency" else "neutral"
            reason = tendency["description"]
        evidence_type = tendency["feature_path"].split(".", 1)[0]
        if evidence_type not in {"rsi", "volume", "ewo", "macd", "atr", "volatility"}:
            evidence_type = "structural"
        evaluations.append(
            {
                "tendency_id": tendency["tendency_id"],
                "classification": classification,
                "evidence": make_evidence_item(
                    evidence_type=evidence_type,
                    feature_name=tendency["feature_path"],
                    observed_value=observed,
                    comparison_or_expected_condition={
                        "operator": tendency["operator"],
                        "threshold": tendency["threshold"],
                    },
                    status=status,
                    reason=reason,
                    source_wave_or_pivot=_field(fingerprint, "identity.wave_id"),
                    comparison_wave_or_pivot=_field(fingerprint, "comparison_summary.reference_wave_id"),
                    hard_rule=False,
                    data_quality_status="available" if observed is not None else "unavailable",
                    provenance=fingerprint.get("source", {}),
                    calculation_version=CALCULATION_VERSION,
                ),
            }
        )
    return {
        "template_version": template["template_version"],
        "role_id": template["role_id"],
        "description": template["description"],
        "hard_rules": [],
        "evaluations": evaluations,
        "policy": "Tendencies are context only and cannot relabel or invalidate the supplied candidate.",
    }


def _overlap_ratio(current: Mapping[str, Any], reference: Mapping[str, Any]) -> float | None:
    current_low = _number(_field(current, "price.lowest_low"))
    current_high = _number(_field(current, "price.highest_high"))
    reference_low = _number(_field(reference, "price.lowest_low"))
    reference_high = _number(_field(reference, "price.highest_high"))
    if None in (current_low, current_high, reference_low, reference_high):
        return None
    intersection = max(0.0, min(current_high, reference_high) - max(current_low, reference_low))
    union = max(current_high, reference_high) - min(current_low, reference_low)
    return intersection / union if union else 1.0


def _apply_comparison(
    current: Mapping[str, Any], reference: Mapping[str, Any], comparison: Mapping[str, Any]
) -> dict[str, Any]:
    result = deepcopy(dict(current))
    relationship = str(comparison.get("relationship") or "comparison")
    reference_id = _field(reference, "identity.wave_id")
    magnitude_ratio = _ratio(
        _number(_field(current, "price.absolute_change")),
        _number(_field(reference, "price.absolute_change")),
    )
    target = "retracement_ratio" if relationship == "retracement" else "extension_ratio"
    result["structural"][target] = {
        "status": "available" if magnitude_ratio is not None else "unavailable",
        "value": magnitude_ratio,
        "reference_wave_id": reference_id,
        "reference_kind": relationship,
        "reason": "Absolute price movement was compared with the explicit structural reference." if magnitude_ratio is not None else "Reference movement is unavailable or zero.",
    }
    result["evidence_items"].append(
        make_evidence_item(
            evidence_type="fibonacci",
            feature_name=f"structural.{target}",
            observed_value=magnitude_ratio,
            comparison_or_expected_condition={
                "reference_wave_id": reference_id,
                "relationship": relationship,
            },
            status="neutral" if magnitude_ratio is not None else "unavailable",
            reason=(
                "A deterministic movement ratio was recorded without treating a Fibonacci tendency as a hard rule."
                if magnitude_ratio is not None
                else "The movement ratio is unavailable because the reference is missing or zero."
            ),
            source_wave_or_pivot=_field(current, "identity.wave_id"),
            comparison_wave_or_pivot=reference_id,
            hard_rule=False,
            data_quality_status="available" if magnitude_ratio is not None else "unavailable",
            provenance=result.get("source", {}),
            calculation_version=CALCULATION_VERSION,
        )
    )
    overlap = _overlap_ratio(current, reference)
    result["structural"]["overlap_ratio"] = {
        "status": "available" if overlap is not None else "unavailable",
        "value": overlap,
        "reference_wave_id": reference_id,
        "reference_kind": "price_range_overlap",
        "reason": "Intersection-over-union of the two observed price ranges." if overlap is not None else "One price range is unavailable.",
    }
    result["comparison_summary"] = {
        field: deepcopy(comparison.get(field))
        for field in (
            "status",
            "relationship",
            "current_wave_id",
            "reference_wave_id",
            "normalized_ratios",
            "directional_differences",
            "divergence_candidates",
            "incomparable_fields",
            "unavailable_reasons",
            "comparison_hash",
        )
    }
    result["evidence_items"].extend(deepcopy(comparison.get("evidence_items", [])))
    for indicator, divergence in comparison.get("divergence_candidates", {}).items():
        block = result.get("indicators", {}).get(indicator)
        if not isinstance(block, dict):
            continue
        block["comparison_identity"] = reference_id
        block["comparability"] = divergence.get("comparability")
        block["aligned_divergence_candidate"] = divergence.get("candidate")
        block["divergence_details"] = deepcopy(divergence)
    if isinstance(result.get("indicators", {}).get("volume"), dict):
        volume_incomparable = any(
            str(item.get("field", "")).startswith("volume")
            for item in comparison.get("incomparable_fields", [])
        )
        result["indicators"]["volume"]["feed_comparability"] = "incomparable" if volume_incomparable else "comparable"
    result["role_template"] = evaluate_role_template(result)
    result.pop("content_hash", None)
    return _finalize_fingerprint(result)


def _default_reference(position: str) -> tuple[str, str] | None:
    references = {
        "2": ("1", "retracement"),
        "3": ("1", "extension"),
        "4": ("3", "retracement"),
        "5": ("3", "extension"),
        "B": ("A", "retracement"),
        "C": ("A", "extension"),
        "X": ("W", "retracement"),
        "Y": ("W", "extension"),
        "X2": ("Y", "retracement"),
        "Z": ("Y", "extension"),
    }
    return references.get(position)


def generate_response_fingerprints(
    response: Mapping[str, Any],
    market_paths: Iterable[Path],
    *,
    workspace: Path,
    features: Iterable[str] | None = None,
    origin_run_id: int | None = None,
) -> dict[str, Any]:
    """Fingerprint all measurable response nodes and compare structural siblings."""
    nodes = [item for item in response.get("degree_hierarchy", []) if isinstance(item, Mapping)]
    node_by_id = {str(node.get("wave_id")): node for node in nodes if node.get("wave_id")}
    paths_by_timeframe: dict[str, Path] = {}
    for raw_path in market_paths:
        path = Path(raw_path).resolve()
        paths_by_timeframe.setdefault(infer_timeframe(path), path)
    candles_cache: dict[Path, list[dict[str, Any]]] = {}
    metadata_cache: dict[Path, dict[str, Any]] = {}
    fingerprints: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    cutoff = response.get("data_cutoff")
    resolved_run_id = origin_run_id or _number(response.get("source_run_id"))

    for node in nodes:
        wave_id = str(node.get("wave_id") or "unknown")
        timeframe = str(node.get("timeframe") or "unknown")
        path = paths_by_timeframe.get(timeframe)
        if path is None:
            unavailable.append({"wave_id": wave_id, "status": "unavailable", "reason": f"No {timeframe} candle file is available."})
            continue
        if path not in candles_cache:
            candles_cache[path] = load_candles(path)
            metadata_cache[path] = load_market_metadata(path)
        parent = node_by_id.get(str(node.get("parent_wave_id")))
        parent_positions = [
            _position(node_by_id[str(child_id)])
            for child_id in parent.get("child_wave_ids", [])
            if parent is not None and str(child_id) in node_by_id
        ] if parent is not None else []
        contextual_node = dict(node)
        contextual_node["parent_pattern_family"] = (
            _detailed_family(parent.get("structure"), parent_positions)
            if parent is not None
            else _detailed_family(node.get("parent_pattern_family"))
        )
        try:
            fingerprint = generate_wave_fingerprint(
                contextual_node,
                candles_cache[path],
                source_metadata=metadata_cache[path],
                cutoff=cutoff or _anchor(node, "end", "date"),
                features=features,
                symbol=str(response.get("symbol") or "unknown"),
                timeframe=timeframe,
                source_path=str(path),
                origin_run_id=int(resolved_run_id) if resolved_run_id is not None else None,
            )
        except (TypeError, ValueError, KeyError) as exc:
            unavailable.append({"wave_id": wave_id, "status": "unavailable", "reason": str(exc)})
            continue
        fingerprints.append(fingerprint)

    by_id = {str(_field(item, "identity.wave_id")): item for item in fingerprints}
    comparisons: list[dict[str, Any]] = []
    final_fingerprints: list[dict[str, Any]] = []
    for fingerprint in fingerprints:
        wave_id = str(_field(fingerprint, "identity.wave_id"))
        node = node_by_id.get(wave_id, {})
        position = str(_field(fingerprint, "identity.wave_label"))
        explicit_reference = node.get("fingerprint_comparison_wave_id")
        default_reference = _default_reference(position)
        reference_id = str(explicit_reference) if explicit_reference else default_reference[0] if default_reference else None
        relationship = str(node.get("fingerprint_comparison_relationship") or (default_reference[1] if default_reference else "comparison"))
        reference = by_id.get(reference_id) if reference_id else None
        if reference is None:
            final_fingerprints.append(fingerprint)
            continue
        comparison = compare_wave_fingerprints(
            fingerprint,
            reference,
            relationship=relationship,
        )
        comparisons.append(comparison)
        final_fingerprints.append(_apply_comparison(fingerprint, reference, comparison))

    return {
        "status": "calculated" if final_fingerprints else "unavailable",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "role_template_version": ROLE_TEMPLATE_VERSION,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "fingerprints": final_fingerprints,
        "comparisons": comparisons,
        "unavailable_fingerprints": unavailable,
        "policy": "Fingerprints describe supplied candidates only and use no candle later than the declared cutoff.",
    }
