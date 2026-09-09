"""Dependency-free professional candle indicators and scale diagnostics."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .evidence import EVIDENCE_CALCULATION_VERSION, make_evidence_item


DEFAULT_CANDLE_FEATURES = ("ewo", "macd", "volatility", "volume", "scale")
SUPPORTED_CANDLE_FEATURES = frozenset((*DEFAULT_CANDLE_FEATURES, "rsi"))
ANALYSIS_PROFILES = {
    "legacy_default": DEFAULT_CANDLE_FEATURES,
    "elliott_full": (*DEFAULT_CANDLE_FEATURES, "rsi"),
}
DEFAULT_RSI_PERIOD = 14
RSI_METHOD_ID = "wilder_sma_seed_recursive"
RSI_METHOD_DESCRIPTION = (
    "Mean gain and loss over the first 14 consecutive changes, followed by "
    "Wilder smoothing: ((previous * 13) + current) / 14."
)
INDICATOR_EVIDENCE_STATUSES = frozenset(
    {"supportive", "contradictory", "neutral", "unavailable", "incomparable"}
)


def normalize_features(features: Iterable[str] | None) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(str(item).strip().lower() for item in (features or ())))
    if not requested:
        return DEFAULT_CANDLE_FEATURES
    invalid = sorted(set(requested) - SUPPORTED_CANDLE_FEATURES)
    if invalid:
        raise ValueError("Unsupported candle features: " + ", ".join(invalid))
    return requested


def features_for_profile(profile: str | None) -> tuple[str, ...]:
    """Resolve a named feature profile without changing the legacy default."""
    name = (profile or "legacy_default").strip().lower()
    try:
        return tuple(ANALYSIS_PROFILES[name])
    except KeyError as exc:
        raise ValueError(f"Unsupported analysis profile: {profile!r}.") from exc


def with_optional_rsi(
    features: Iterable[str] | None, *, enabled: bool
) -> tuple[str, ...]:
    """Add RSI to the selected feature set without changing default selection rules."""
    requested = tuple(features or ())
    if not enabled:
        return requested
    active = list(normalize_features(requested))
    if "rsi" not in active:
        active.append("rsi")
    return tuple(active)


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _validate_length(length: int) -> None:
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        raise ValueError("Indicator length must be a positive integer.")


def simple_moving_average(
    values: Iterable[Any], length: int
) -> list[float | None]:
    """Return a contiguous-window SMA; invalid inputs make that window unavailable."""
    _validate_length(length)
    normalized = [_finite_number(value) for value in values]
    result: list[float | None] = [None] * len(normalized)
    running = 0.0
    valid_count = 0
    for index, value in enumerate(normalized):
        if value is not None:
            running += value
            valid_count += 1
        if index >= length:
            leaving = normalized[index - length]
            if leaving is not None:
                running -= leaving
                valid_count -= 1
        if index >= length - 1 and valid_count == length:
            result[index] = running / length
    return result


def _rolling_std(values: list[float], length: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(length - 1, len(values)):
        window = values[index - length + 1 : index + 1]
        mean = sum(window) / length
        result[index] = math.sqrt(sum((value - mean) ** 2 for value in window) / length)
    return result


def exponential_moving_average(
    values: Iterable[Any], length: int
) -> list[float | None]:
    """Return an SMA-seeded EMA, restarting after invalid or missing values."""
    _validate_length(length)
    normalized = [_finite_number(value) for value in values]
    result: list[float | None] = [None] * len(normalized)
    alpha = 2.0 / (length + 1.0)
    segment: list[float] = []
    previous: float | None = None
    for index, value in enumerate(normalized):
        if value is None:
            segment = []
            previous = None
            continue
        segment.append(value)
        if len(segment) < length:
            continue
        if len(segment) == length:
            previous = sum(segment) / length
        else:
            previous = alpha * value + (1.0 - alpha) * float(previous)
        result[index] = previous
    return result


def exponential_moving_average_optional(
    values: Iterable[Any], length: int
) -> list[float | None]:
    return exponential_moving_average(values, length)


def wilder_moving_average(
    values: Iterable[Any], length: int
) -> list[float | None]:
    """Return an SMA-seeded Wilder moving average with contiguous gap resets."""
    _validate_length(length)
    normalized = [_finite_number(value) for value in values]
    result: list[float | None] = [None] * len(normalized)
    segment: list[float] = []
    previous: float | None = None
    for index, value in enumerate(normalized):
        if value is None:
            segment = []
            previous = None
            continue
        segment.append(value)
        if len(segment) < length:
            continue
        if len(segment) == length:
            previous = sum(segment) / length
        else:
            previous = ((length - 1) * float(previous) + value) / length
        result[index] = previous
    return result


def calculate_ewo(
    highs: Iterable[Any],
    lows: Iterable[Any],
    *,
    fast_period: int = 5,
    slow_period: int = 35,
) -> dict[str, list[float | None]]:
    """Calculate canonical EWO from SMA(fast)-SMA(slow) of median price."""
    high_values = [_finite_number(value) for value in highs]
    low_values = [_finite_number(value) for value in lows]
    if len(high_values) != len(low_values):
        raise ValueError("High and low series must have the same length.")
    medians = [
        (float(high) + float(low)) / 2.0
        if high is not None and low is not None
        else None
        for high, low in zip(high_values, low_values)
    ]
    fast = simple_moving_average(medians, fast_period)
    slow = simple_moving_average(medians, slow_period)
    values = [
        float(fast_value) - float(slow_value)
        if fast_value is not None and slow_value is not None
        else None
        for fast_value, slow_value in zip(fast, slow)
    ]
    return {"median_price": medians, "fast_sma": fast, "slow_sma": slow, "values": values}


def calculate_macd(
    closes: Iterable[Any],
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict[str, list[float | None]]:
    """Calculate canonical SMA-seeded MACD and signal series."""
    close_values = list(closes)
    fast = exponential_moving_average(close_values, fast_period)
    slow = exponential_moving_average(close_values, slow_period)
    line = [
        float(fast_value) - float(slow_value)
        if fast_value is not None and slow_value is not None
        else None
        for fast_value, slow_value in zip(fast, slow)
    ]
    signal = exponential_moving_average_optional(line, signal_period)
    histogram = [
        float(line_value) - float(signal_value)
        if line_value is not None and signal_value is not None
        else None
        for line_value, signal_value in zip(line, signal)
    ]
    return {
        "fast_ema": fast,
        "slow_ema": slow,
        "line": line,
        "signal": signal,
        "histogram": histogram,
    }


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_gain == 0.0 and average_loss == 0.0:
        return 50.0
    if average_loss == 0.0:
        return 100.0
    if average_gain == 0.0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def calculate_wilder_rsi(
    closes: Iterable[Any], *, period: int = DEFAULT_RSI_PERIOD
) -> dict[str, Any]:
    """Calculate Wilder RSI with a period-change SMA seed and recursive smoothing.

    A value first becomes available after ``period`` consecutive price changes.
    Missing or non-finite closes are marked invalid and reset the contiguous
    initialization window; values are never bridged across a data gap.
    """
    _validate_length(period)
    normalized = [_finite_number(value) for value in closes]
    values: list[float | None] = [None] * len(normalized)
    statuses: list[str] = ["warming_up"] * len(normalized)
    previous_close: float | None = None
    gains: list[float] = []
    losses: list[float] = []
    average_gain: float | None = None
    average_loss: float | None = None

    for index, close in enumerate(normalized):
        if close is None:
            statuses[index] = "invalid_input"
            previous_close = None
            gains = []
            losses = []
            average_gain = None
            average_loss = None
            continue
        if previous_close is None:
            previous_close = close
            continue
        change = close - previous_close
        previous_close = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if average_gain is None or average_loss is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) < period:
                continue
            average_gain = sum(gains) / period
            average_loss = sum(losses) / period
        else:
            average_gain = ((period - 1) * average_gain + gain) / period
            average_loss = ((period - 1) * average_loss + loss) / period
        values[index] = _rsi_from_averages(average_gain, average_loss)
        statuses[index] = "ready"

    return {
        "values": values,
        "statuses": statuses,
        "period": period,
        "method": RSI_METHOD_ID,
        "method_description": (
            f"Mean gain and loss over the first {period} consecutive changes, "
            f"followed by Wilder smoothing: ((previous * {period - 1}) + current) "
            f"/ {period}."
        ),
    }


def wilder_rsi(
    closes: Iterable[Any], period: int = DEFAULT_RSI_PERIOD
) -> list[float | None]:
    """Compatibility list API for the canonical Wilder RSI implementation."""
    return calculate_wilder_rsi(closes, period=period)["values"]


def indicator_evidence(
    status: str,
    observation: str,
    *,
    supporting: Iterable[str] = (),
    contradictory: Iterable[str] = (),
    unavailable_or_incomparable: Iterable[str] = (),
    evidence_needed: Iterable[str] = (),
) -> dict[str, Any]:
    """Build unified indicator evidence plus Phase 2 compatibility aliases."""
    normalized = str(status).strip().lower()
    if normalized not in INDICATOR_EVIDENCE_STATUSES:
        raise ValueError(f"Unsupported indicator evidence status: {status!r}")
    unavailable = [str(item) for item in unavailable_or_incomparable]
    needed = [str(item) for item in evidence_needed]
    unified = make_evidence_item(
        evidence_type="indicator",
        feature_name="indicator.observation",
        observed_value=str(observation),
        comparison_or_expected_condition=needed or None,
        status=normalized,
        reason=str(observation),
        hard_rule=False,
        data_quality_status=(
            normalized if normalized in {"unavailable", "incomparable"} else "available"
        ),
        calculation_version=EVIDENCE_CALCULATION_VERSION,
    )
    return {
        **unified,
        "observation": str(observation),
        "supporting_evidence": [str(item) for item in supporting],
        "contradictory_evidence": [str(item) for item in contradictory],
        "unavailable_or_incomparable_evidence": unavailable,
        "evidence_needed_for_confirmation": needed,
        "structural_invalidation": False,
        "policy": "Indicator evidence cannot invalidate an Elliott count by itself.",
    }


def _slope_with_gaps(values: list[float | None]) -> float | None:
    points = [
        (float(index), float(value))
        for index, value in enumerate(values)
        if value is not None
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


def summarize_rsi_window(
    values: Iterable[Any], statuses: Iterable[str] | None = None
) -> dict[str, Any]:
    """Summarize RSI values for one already aligned wave interval."""
    normalized = [_finite_number(value) for value in values]
    quality = list(statuses or ())
    if len(quality) != len(normalized):
        quality = ["ready" if value is not None else "warming_up" for value in normalized]
    available = [float(value) for value in normalized if value is not None]
    above = sum(value > 50.0 for value in available)
    below = sum(value < 50.0 for value in available)
    at_midline = sum(value == 50.0 for value in available)
    availability = "available" if available else "unavailable"
    if not normalized:
        warm_up_status = "no_bars"
    elif len(available) == len(normalized):
        warm_up_status = "complete"
    elif available:
        warm_up_status = "partial"
    elif any(status == "invalid_input" for status in quality):
        warm_up_status = "invalid_or_missing_input"
    else:
        warm_up_status = "insufficient_warm_up"
    observation = (
        f"RSI is available on {len(available)} of {len(normalized)} wave bars."
        if available
        else "RSI is unavailable for this wave interval."
    )
    evidence = (
        indicator_evidence("neutral", observation)
        if available
        else indicator_evidence(
            "unavailable",
            observation,
            unavailable_or_incomparable=(
                "No post-warm-up RSI value exists inside the aligned wave interval.",
            ),
        )
    )
    return {
        "availability": availability,
        "warm_up_status": warm_up_status,
        "available_bars": len(available),
        "total_wave_bars": len(normalized),
        "value_at_wave_start": normalized[0] if normalized else None,
        "value_at_wave_end": normalized[-1] if normalized else None,
        "minimum": min(available, default=None),
        "maximum": max(available, default=None),
        "slope_per_bar": _slope_with_gaps(normalized),
        "bars_above_50": above,
        "bars_below_50": below,
        "bars_at_50": at_midline,
        "proportion_above_50": above / len(available) if available else None,
        "proportion_below_50": below / len(available) if available else None,
        "comparison_with_prior_wave": None,
        "comparability": "unavailable",
        "divergence_candidate": None,
        "divergence_strength_rsi_points": None,
        "divergence_distance": None,
        "evidence": evidence,
    }


def _known_source_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if not text or text.lower() == "unknown" else text


def _source_comparability(
    current: Mapping[str, Any], prior: Mapping[str, Any]
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    required = (
        "feed_identity",
        "timeframe",
        "price_field",
        "period",
        "calculation_method",
    )
    missing = [
        key
        for key in required
        if _known_source_value(current.get(key)) is None
        or _known_source_value(prior.get(key)) is None
    ]
    if missing:
        return "unavailable", ["Missing source metadata: " + ", ".join(missing) + "."]
    for key in required:
        if str(current.get(key)) != str(prior.get(key)):
            reasons.append(
                f"{key} differs ({current.get(key)!r} versus {prior.get(key)!r})."
            )
    for key in ("provider", "venue", "symbol"):
        current_value = _known_source_value(current.get(key))
        prior_value = _known_source_value(prior.get(key))
        if current_value is not None and prior_value is not None and current_value != prior_value:
            reasons.append(
                f"{key} differs ({current_value!r} versus {prior_value!r})."
            )
    return ("incomparable", reasons) if reasons else ("comparable", [])


def compare_rsi_pivots(
    current: Mapping[str, Any],
    prior: Mapping[str, Any],
    *,
    expected_divergence: str | None = None,
) -> dict[str, Any]:
    """Compare two explicit aligned pivots without inferring comparability from labels."""
    current_alignment = current.get("pivot_alignment")
    prior_alignment = prior.get("pivot_alignment")
    aligned = (
        isinstance(current_alignment, Mapping)
        and isinstance(prior_alignment, Mapping)
        and current_alignment.get("status") == "aligned"
        and prior_alignment.get("status") == "aligned"
    )
    if not aligned:
        reason = "Both RSI endpoints must align with their supplied price pivots."
        return {
            "prior_wave_id": prior.get("wave_id"),
            "comparability": "incomparable",
            "divergence_candidate": None,
            "divergence_strength_rsi_points": None,
            "divergence_distance": None,
            "evidence": indicator_evidence(
                "incomparable",
                "RSI divergence was not evaluated because pivot alignment failed.",
                unavailable_or_incomparable=(reason,),
            ),
        }

    current_source = current.get("source")
    prior_source = prior.get("source")
    if not isinstance(current_source, Mapping) or not isinstance(prior_source, Mapping):
        source_status, source_reasons = "unavailable", ["RSI source metadata is missing."]
    else:
        source_status, source_reasons = _source_comparability(current_source, prior_source)
    if source_status != "comparable":
        return {
            "prior_wave_id": prior.get("wave_id"),
            "comparability": source_status,
            "divergence_candidate": None,
            "divergence_strength_rsi_points": None,
            "divergence_distance": None,
            "evidence": indicator_evidence(
                source_status,
                "RSI divergence was not evaluated because the sources are not comparable.",
                unavailable_or_incomparable=source_reasons,
            ),
        }

    current_price = _finite_number(current.get("end_pivot_price"))
    prior_price = _finite_number(prior.get("end_pivot_price"))
    current_rsi = _finite_number(current.get("value_at_wave_end"))
    prior_rsi = _finite_number(prior.get("value_at_wave_end"))
    if None in (current_price, prior_price, current_rsi, prior_rsi) or prior_price == 0:
        reason = "Aligned endpoint price and RSI values are required for both pivots."
        return {
            "prior_wave_id": prior.get("wave_id"),
            "comparability": "unavailable",
            "divergence_candidate": None,
            "divergence_strength_rsi_points": None,
            "divergence_distance": None,
            "evidence": indicator_evidence(
                "unavailable",
                "RSI divergence could not be calculated.",
                unavailable_or_incomparable=(reason,),
            ),
        }

    price_distance_pct = (float(current_price) / float(prior_price) - 1.0) * 100.0
    rsi_distance = float(current_rsi) - float(prior_rsi)
    if current_price < prior_price and current_rsi > prior_rsi:
        candidate = "bullish"
    elif current_price > prior_price and current_rsi < prior_rsi:
        candidate = "bearish"
    else:
        candidate = "none"
    expected = str(expected_divergence or "").strip().lower() or None
    if expected is not None and expected not in {"bullish", "bearish"}:
        raise ValueError("expected_divergence must be bullish, bearish, or null.")
    if expected is None:
        evidence_status = "neutral"
        supporting: tuple[str, ...] = ()
        contradictory: tuple[str, ...] = ()
    elif candidate == expected:
        evidence_status = "supportive"
        supporting = (f"Aligned pivots form a {candidate} RSI divergence candidate.",)
        contradictory = ()
    else:
        evidence_status = "contradictory"
        supporting = ()
        contradictory = (
            f"Expected {expected} divergence; aligned pivots produced {candidate}.",
        )
    return {
        "prior_wave_id": prior.get("wave_id"),
        "comparability": "comparable",
        "divergence_candidate": candidate,
        "divergence_strength_rsi_points": (
            abs(rsi_distance) if candidate in {"bullish", "bearish"} else None
        ),
        "divergence_distance": {
            "price_percent": price_distance_pct,
            "rsi_points": rsi_distance,
        },
        "evidence": indicator_evidence(
            evidence_status,
            f"Aligned comparable pivots produced {candidate} RSI divergence.",
            supporting=supporting,
            contradictory=contradictory,
        ),
    }


def _position(value: float, lower: float | None, upper: float | None) -> float | None:
    if lower is None or upper is None or upper == lower:
        return None
    return (value - lower) / (upper - lower)


def enrich_candles(
    candles: Iterable[Mapping[str, Any]],
    *,
    features: Iterable[str] | None = None,
    rsi_period: int = DEFAULT_RSI_PERIOD,
) -> list[dict[str, Any]]:
    """Add selected canonical indicators; RSI fields exist only when enabled."""
    enabled = set(normalize_features(features))
    enriched = [dict(candle) for candle in candles]
    if not enriched:
        return enriched
    closes = [float(row["close"]) for row in enriched]
    highs = [float(row["high"]) for row in enriched]
    lows = [float(row["low"]) for row in enriched]
    medians = [(high + low) / 2.0 for high, low in zip(highs, lows)]
    volumes = [
        float(row["volume"]) if row.get("volume") is not None else 0.0
        for row in enriched
    ]

    if "ewo" in enabled:
        ewo_calculation = calculate_ewo(highs, lows)
        sma_5 = ewo_calculation["fast_sma"]
        sma_35 = ewo_calculation["slow_sma"]
        ewo_values = ewo_calculation["values"]
    else:
        sma_5 = [None] * len(enriched)
        sma_35 = [None] * len(enriched)
        ewo_values = [None] * len(enriched)
    if "macd" in enabled:
        macd_calculation = calculate_macd(closes)
        macd = macd_calculation["line"]
        macd_signal = macd_calculation["signal"]
        macd_histogram = macd_calculation["histogram"]
    else:
        macd = [None] * len(enriched)
        macd_signal = [None] * len(enriched)
        macd_histogram = [None] * len(enriched)
    rsi_calculation = (
        calculate_wilder_rsi(closes, period=rsi_period)
        if "rsi" in enabled
        else None
    )

    true_ranges: list[float] = []
    for index, (high, low) in enumerate(zip(highs, lows)):
        previous_close = closes[index - 1] if index else closes[index]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr_14 = wilder_moving_average(true_ranges, 14) if "volatility" in enabled else [None] * len(enriched)
    bb_mean = simple_moving_average(closes, 20) if "volatility" in enabled else [None] * len(enriched)
    bb_std = _rolling_std(closes, 20) if "volatility" in enabled else [None] * len(enriched)
    keltner_middle = exponential_moving_average(closes, 20) if "volatility" in enabled else [None] * len(enriched)
    volume_mean_50 = simple_moving_average(volumes, 50) if "volume" in enabled else [None] * len(enriched)

    for index, row in enumerate(enriched):
        close = closes[index]
        row["median_price"] = medians[index]
        row["log_close"] = math.log(close) if close > 0 else None
        row["return_pct"] = (
            (close / closes[index - 1] - 1.0) * 100.0
            if index and closes[index - 1] != 0
            else None
        )
        row["log_return"] = (
            math.log(close / closes[index - 1])
            if index and close > 0 and closes[index - 1] > 0
            else None
        )
        row["ewo"] = ewo_values[index]
        row["ewo_normalized_pct"] = (
            float(row["ewo"]) / close * 100.0 if row["ewo"] is not None and close else None
        )
        row["macd"] = macd[index]
        row["macd_signal"] = macd_signal[index]
        row["macd_histogram"] = macd_histogram[index]
        if rsi_calculation is not None:
            row["rsi"] = rsi_calculation["values"][index]
            row["rsi_status"] = rsi_calculation["statuses"][index]
            row["rsi_period"] = rsi_period
        row["atr_14"] = atr_14[index]
        row["atr_pct"] = (
            float(atr_14[index]) / close * 100.0 if atr_14[index] is not None and close else None
        )
        if bb_mean[index] is not None and bb_std[index] is not None:
            row["bollinger_middle"] = bb_mean[index]
            row["bollinger_upper"] = float(bb_mean[index]) + 2.0 * float(bb_std[index])
            row["bollinger_lower"] = float(bb_mean[index]) - 2.0 * float(bb_std[index])
        else:
            row["bollinger_middle"] = None
            row["bollinger_upper"] = None
            row["bollinger_lower"] = None
        row["bollinger_position"] = _position(
            close, row["bollinger_lower"], row["bollinger_upper"]
        )
        if keltner_middle[index] is not None and atr_14[index] is not None:
            row["keltner_middle"] = keltner_middle[index]
            row["keltner_upper"] = float(keltner_middle[index]) + 2.0 * float(atr_14[index])
            row["keltner_lower"] = float(keltner_middle[index]) - 2.0 * float(atr_14[index])
        else:
            row["keltner_middle"] = None
            row["keltner_upper"] = None
            row["keltner_lower"] = None
        row["keltner_position"] = _position(
            close, row["keltner_lower"], row["keltner_upper"]
        )
        average_volume = volume_mean_50[index]
        row["volume_ratio_50"] = (
            volumes[index] / float(average_volume)
            if average_volume is not None and average_volume > 0 and row.get("volume") is not None
            else None
        )
    return enriched


def _crosses(left: list[float | None], right: list[float | None]) -> dict[str, int]:
    bullish = 0
    bearish = 0
    for previous_index, index in zip(range(len(left) - 1), range(1, len(left))):
        values = (left[previous_index], right[previous_index], left[index], right[index])
        if any(value is None for value in values):
            continue
        if float(left[previous_index]) <= float(right[previous_index]) and float(left[index]) > float(right[index]):
            bullish += 1
        elif float(left[previous_index]) >= float(right[previous_index]) and float(left[index]) < float(right[index]):
            bearish += 1
    return {"bullish": bullish, "bearish": bearish}


def _linear_fit(values: list[float]) -> dict[str, Any]:
    count = len(values)
    if count < 3:
        return {"available": False}
    mean_x = (count - 1) / 2.0
    mean_y = sum(values) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    slope = (
        sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
        / denominator
        if denominator
        else 0.0
    )
    intercept = mean_y - slope * mean_x
    predictions = [intercept + slope * index for index in range(count)]
    residuals = [value - prediction for value, prediction in zip(values, predictions)]
    residual_std = math.sqrt(sum(value * value for value in residuals) / count)
    total = sum((value - mean_y) ** 2 for value in values)
    residual_total = sum(value * value for value in residuals)
    r_squared = 1.0 - residual_total / total if total else 1.0
    return {
        "available": True,
        "slope": slope,
        "intercept": intercept,
        "r_squared": r_squared,
        "residual_std": residual_std,
        "predictions": predictions,
        "residuals": residuals,
    }


def compare_price_scales(candles: Iterable[Mapping[str, Any]], *, window: int = 120) -> dict[str, Any]:
    rows = list(candles)[-max(3, window) :]
    closes = [float(row["close"]) for row in rows if float(row["close"]) > 0]
    if len(closes) < 3:
        return {"status": "insufficient_data", "window_bars": len(closes)}
    arithmetic = _linear_fit(closes)
    logarithmic = _linear_fit([math.log(value) for value in closes])
    arithmetic_predictions = arithmetic.pop("predictions")
    arithmetic.pop("residuals")
    log_predictions = logarithmic.pop("predictions")
    logarithmic.pop("residuals")
    arithmetic_mape = sum(
        abs(actual - predicted) / actual
        for actual, predicted in zip(closes, arithmetic_predictions)
    ) / len(closes)
    log_price_predictions = [math.exp(value) for value in log_predictions]
    logarithmic_mape = sum(
        abs(actual - predicted) / actual
        for actual, predicted in zip(closes, log_price_predictions)
    ) / len(closes)
    latest_arithmetic_z = (
        (closes[-1] - arithmetic_predictions[-1]) / arithmetic["residual_std"]
        if arithmetic["residual_std"]
        else 0.0
    )
    latest_log_z = (
        (math.log(closes[-1]) - log_predictions[-1]) / logarithmic["residual_std"]
        if logarithmic["residual_std"]
        else 0.0
    )
    relative_difference = abs(arithmetic_mape - logarithmic_mape) / max(
        arithmetic_mape, logarithmic_mape, 1e-12
    )
    preferred = (
        "mixed"
        if relative_difference < 0.05
        else "arithmetic"
        if arithmetic_mape < logarithmic_mape
        else "logarithmic"
    )
    arithmetic.update(
        {
            "mean_absolute_percentage_error": arithmetic_mape,
            "latest_channel_z": latest_arithmetic_z,
            "channel_width_2sigma": 2.0 * arithmetic["residual_std"],
        }
    )
    logarithmic.update(
        {
            "mean_absolute_percentage_error": logarithmic_mape,
            "latest_channel_z": latest_log_z,
            "channel_width_2sigma_log": 2.0 * logarithmic["residual_std"],
            "compound_slope_pct_per_bar": (math.exp(logarithmic["slope"]) - 1.0) * 100.0,
        }
    )
    return {
        "status": "calculated",
        "window_bars": len(closes),
        "preferred_scale": preferred,
        "selection_rule": "Lower price-space mean absolute percentage regression error; mixed within 5%.",
        "arithmetic": arithmetic,
        "logarithmic": logarithmic,
    }


def summarize_indicators(
    candles: Iterable[Mapping[str, Any]],
    *,
    features: Iterable[str] | None = None,
) -> dict[str, Any]:
    enabled = set(normalize_features(features))
    rows = list(candles)
    result: dict[str, Any] = {"active_features": sorted(enabled)}
    if "ewo" in enabled:
        values = [float(row["ewo"]) for row in rows if row.get("ewo") is not None]
        normalized = [
            float(row["ewo_normalized_pct"])
            for row in rows
            if row.get("ewo_normalized_pct") is not None
        ]
        result["ewo"] = {
            "available_bars": len(values),
            "latest": values[-1] if values else None,
            "absolute_peak": max((abs(value) for value in values), default=None),
            "latest_normalized_pct": normalized[-1] if normalized else None,
            "normalized_absolute_peak_pct": max((abs(value) for value in normalized), default=None),
        }
    if "macd" in enabled:
        lines = [row.get("macd") for row in rows]
        signals = [row.get("macd_signal") for row in rows]
        histograms = [float(row["macd_histogram"]) for row in rows if row.get("macd_histogram") is not None]
        result["macd"] = {
            "available_bars": len(histograms),
            "latest_line": next((float(row["macd"]) for row in reversed(rows) if row.get("macd") is not None), None),
            "latest_signal": next((float(row["macd_signal"]) for row in reversed(rows) if row.get("macd_signal") is not None), None),
            "latest_histogram": histograms[-1] if histograms else None,
            "histogram_positive_peak": max(histograms, default=None),
            "histogram_negative_peak": min(histograms, default=None),
            "signal_crosses": _crosses(lines, signals),
        }
    if "rsi" in enabled:
        values = [float(row["rsi"]) for row in rows if row.get("rsi") is not None]
        statuses = [str(row.get("rsi_status") or "unknown") for row in rows]
        invalid_bars = sum(status == "invalid_input" for status in statuses)
        warming_bars = sum(status == "warming_up" for status in statuses)
        result["rsi"] = {
            "availability": "available" if values else "unavailable",
            "available_bars": len(values),
            "latest": values[-1] if values else None,
            "minimum": min(values, default=None),
            "maximum": max(values, default=None),
            "period": next(
                (
                    int(row["rsi_period"])
                    for row in reversed(rows)
                    if row.get("rsi_period") is not None
                ),
                DEFAULT_RSI_PERIOD,
            ),
            "calculation_method": RSI_METHOD_ID,
            "method_description": RSI_METHOD_DESCRIPTION,
            "warm_up_status": (
                "ready"
                if values
                else "invalid_or_missing_input"
                if invalid_bars
                else "insufficient_warm_up"
            ),
            "warming_bars": warming_bars,
            "invalid_input_bars": invalid_bars,
            "evidence": (
                indicator_evidence("neutral", "RSI was calculated as optional evidence.")
                if values
                else indicator_evidence(
                    "unavailable",
                    "RSI was enabled but no post-warm-up value is available.",
                    unavailable_or_incomparable=(
                        "The available series did not contain enough consecutive valid closes.",
                    ),
                )
            ),
        }
    if "volatility" in enabled:
        atr_values = [float(row["atr_pct"]) for row in rows if row.get("atr_pct") is not None]
        bb = [float(row["bollinger_position"]) for row in rows if row.get("bollinger_position") is not None]
        kc = [float(row["keltner_position"]) for row in rows if row.get("keltner_position") is not None]
        result["volatility"] = {
            "atr_pct_latest": atr_values[-1] if atr_values else None,
            "atr_pct_average": sum(atr_values) / len(atr_values) if atr_values else None,
            "bollinger_outer_pierces": sum(value < 0 or value > 1 for value in bb),
            "keltner_outer_pierces": sum(value < 0 or value > 1 for value in kc),
            "latest_bollinger_position": bb[-1] if bb else None,
            "latest_keltner_position": kc[-1] if kc else None,
        }
    if "volume" in enabled:
        ratios = [float(row["volume_ratio_50"]) for row in rows if row.get("volume_ratio_50") is not None]
        result["normalized_volume"] = {
            "available_bars": len(ratios),
            "latest_ratio_to_50_period_average": ratios[-1] if ratios else None,
            "peak_ratio": max(ratios, default=None),
        }
    if "scale" in enabled:
        result["scale_comparison"] = compare_price_scales(rows)
    return result
