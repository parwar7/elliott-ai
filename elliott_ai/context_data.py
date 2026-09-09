"""Optional external analytical context adapters with explicit availability states."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .market_data import parse_date, parse_number


CONTEXT_TYPES = (
    "benchmark",
    "breadth",
    "yields",
    "options",
    "fundamentals",
    "news",
    "order_book",
)


def _rows(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("rows", "data", "records", "items", "contracts", "snapshots"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _date(row: Mapping[str, Any]) -> str | None:
    for key in ("date", "datetime", "timestamp", "period", "published_at"):
        if row.get(key) is not None:
            parsed = parse_date(row.get(key))
            return parsed.isoformat() if parsed else str(row.get(key))
    return None


def _number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = parse_number(row.get(key), allow_suffix=True)
        if value is not None:
            return value
    return None


def _percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current / float(previous) - 1.0) * 100.0


def _pearson(left: list[float], right: list[float]) -> float | None:
    count = min(len(left), len(right))
    if count < 3:
        return None
    left = left[-count:]
    right = right[-count:]
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    numerator = sum(
        (x - mean_left) * (y - mean_right) for x, y in zip(left, right)
    )
    denominator = math.sqrt(
        sum((x - mean_left) ** 2 for x in left)
        * sum((y - mean_right) ** 2 for y in right)
    )
    return numerator / denominator if denominator else None


def _return_series(rows: list[tuple[str, float]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for (previous_date, previous), (date, current) in zip(rows, rows[1:]):
        del previous_date
        if previous:
            result[date[:10]] = current / previous - 1.0
    return result


def _summarize_benchmark(
    value: Mapping[str, Any], asset_candles: Iterable[Mapping[str, Any]] | None
) -> dict[str, Any]:
    benchmark_rows = sorted(
        [
            (date, close)
            for row in _rows(value)
            if (date := _date(row)) is not None
            and (close := _number(row, "close", "value", "price")) is not None
        ],
        key=lambda item: item[0],
    )
    if len(benchmark_rows) < 2:
        return {"status": "insufficient_data", "reason": "At least two benchmark closes are required."}
    summary: dict[str, Any] = {
        "status": "calculated",
        "symbol": value.get("symbol"),
        "bars": len(benchmark_rows),
        "first_close": benchmark_rows[0][1],
        "last_close": benchmark_rows[-1][1],
        "total_return_pct": _percent_change(benchmark_rows[-1][1], benchmark_rows[0][1]),
    }
    if asset_candles:
        asset_rows = sorted(
            [
                (
                    row["parsed_date"].date().isoformat(),
                    float(row["close"]),
                )
                for row in asset_candles
                if row.get("parsed_date") is not None
            ],
            key=lambda item: item[0],
        )
        asset_returns = _return_series(asset_rows)
        benchmark_returns = _return_series(benchmark_rows)
        overlap = sorted(set(asset_returns) & set(benchmark_returns))
        if overlap:
            asset_values = [asset_returns[date] for date in overlap]
            benchmark_values = [benchmark_returns[date] for date in overlap]
            ratios = [
                (1.0 + asset_returns[date]) / (1.0 + benchmark_returns[date])
                for date in overlap
                if benchmark_returns[date] > -1.0
            ]
            summary["relative_strength"] = {
                "overlap_bars": len(overlap),
                "asset_benchmark_return_correlation": _pearson(
                    asset_values, benchmark_values
                ),
                "average_period_relative_return_ratio": (
                    sum(ratios) / len(ratios) if ratios else None
                ),
                "latest_period_asset_return_pct": asset_values[-1] * 100.0,
                "latest_period_benchmark_return_pct": benchmark_values[-1] * 100.0,
            }
    return summary


def _summarize_breadth(value: Mapping[str, Any]) -> dict[str, Any]:
    calculated: list[dict[str, Any]] = []
    cumulative = 0.0
    for row in sorted(_rows(value), key=lambda item: _date(item) or ""):
        advances = _number(row, "advances", "advancing")
        declines = _number(row, "declines", "declining")
        if advances is None or declines is None:
            continue
        net = advances - declines
        cumulative += net
        new_highs = _number(row, "new_highs", "highs")
        new_lows = _number(row, "new_lows", "lows")
        up_volume = _number(row, "up_volume", "advancing_volume")
        down_volume = _number(row, "down_volume", "declining_volume")
        calculated.append(
            {
                "date": _date(row),
                "advance_decline_net": net,
                "advance_decline_line": cumulative,
                "advance_decline_ratio": advances / declines if declines else None,
                "new_high_new_low_net": (
                    new_highs - new_lows
                    if new_highs is not None and new_lows is not None
                    else None
                ),
                "up_down_volume_ratio": (
                    up_volume / down_volume
                    if up_volume is not None and down_volume not in (None, 0)
                    else None
                ),
            }
        )
    if not calculated:
        return {"status": "insufficient_data", "reason": "Advance and decline rows are required."}
    latest = calculated[-1]
    positive = sum(row["advance_decline_net"] > 0 for row in calculated[-20:])
    return {
        "status": "calculated",
        "bars": len(calculated),
        "latest": latest,
        "positive_breadth_sessions_last_20": positive,
        "breadth_regime": "expanding" if positive > 10 else "contracting" if positive < 10 else "balanced",
    }


def _summarize_yields(value: Mapping[str, Any]) -> dict[str, Any]:
    calculated: list[dict[str, Any]] = []
    for row in sorted(_rows(value), key=lambda item: _date(item) or ""):
        two = _number(row, "yield_2y", "two_year", "2y")
        ten = _number(row, "yield_10y", "ten_year", "10y")
        real_ten = _number(row, "real_yield_10y", "real_10y")
        if two is None and ten is None and real_ten is None:
            continue
        calculated.append(
            {
                "date": _date(row),
                "yield_2y": two,
                "yield_10y": ten,
                "real_yield_10y": real_ten,
                "spread_10y_2y": ten - two if ten is not None and two is not None else None,
            }
        )
    if not calculated:
        return {"status": "insufficient_data", "reason": "No recognized yield fields were found."}
    latest = calculated[-1]
    previous = calculated[-2] if len(calculated) > 1 else {}
    changes = {
        key + "_change": (
            latest[key] - previous[key]
            if latest.get(key) is not None and previous.get(key) is not None
            else None
        )
        for key in ("yield_2y", "yield_10y", "real_yield_10y", "spread_10y_2y")
    }
    return {"status": "calculated", "bars": len(calculated), "latest": latest, "latest_changes": changes}


def _summarize_options(value: Mapping[str, Any]) -> dict[str, Any]:
    contracts = _rows(value)
    if not contracts:
        return {"status": "insufficient_data", "reason": "No options contracts were supplied."}
    underlying = parse_number(value.get("underlying_price"))
    totals = defaultdict(float)
    iv_by_side: dict[str, list[float]] = defaultdict(list)
    oi_by_strike: dict[float, float] = defaultdict(float)
    expiry_iv: dict[str, list[float]] = defaultdict(list)
    gross_gamma = 0.0
    recognized = 0
    for contract in contracts:
        side = str(contract.get("type") or contract.get("option_type") or "").lower()
        if side not in {"call", "put"}:
            continue
        recognized += 1
        volume = _number(contract, "volume") or 0.0
        open_interest = _number(contract, "open_interest", "oi") or 0.0
        strike = _number(contract, "strike")
        iv = _number(contract, "implied_volatility", "iv")
        gamma = _number(contract, "gamma")
        multiplier = _number(contract, "contract_multiplier") or 100.0
        totals[f"{side}_volume"] += volume
        totals[f"{side}_open_interest"] += open_interest
        if strike is not None:
            oi_by_strike[strike] += open_interest
        if iv is not None:
            iv_by_side[side].append(iv)
            expiry = str(contract.get("expiration") or contract.get("expiry") or "unknown")
            expiry_iv[expiry].append(iv)
        if gamma is not None and underlying is not None:
            gross_gamma += abs(gamma) * open_interest * multiplier * underlying * underlying * 0.01
    if not recognized:
        return {"status": "insufficient_data", "reason": "No call/put contracts were recognized."}
    call_oi = totals["call_open_interest"]
    put_oi = totals["put_open_interest"]
    call_volume = totals["call_volume"]
    put_volume = totals["put_volume"]
    concentrations = sorted(oi_by_strike.items(), key=lambda item: item[1], reverse=True)[:10]
    term_structure = {
        expiry: sum(values) / len(values) for expiry, values in sorted(expiry_iv.items()) if values
    }
    call_iv = sum(iv_by_side["call"]) / len(iv_by_side["call"]) if iv_by_side["call"] else None
    put_iv = sum(iv_by_side["put"]) / len(iv_by_side["put"]) if iv_by_side["put"] else None
    return {
        "status": "calculated",
        "recognized_contracts": recognized,
        "put_call_volume_ratio": put_volume / call_volume if call_volume else None,
        "put_call_open_interest_ratio": put_oi / call_oi if call_oi else None,
        "average_call_iv": call_iv,
        "average_put_iv": put_iv,
        "put_minus_call_iv": put_iv - call_iv if put_iv is not None and call_iv is not None else None,
        "open_interest_concentrations": [
            {"strike": strike, "open_interest": open_interest}
            for strike, open_interest in concentrations
        ],
        "iv_term_structure": term_structure,
        "gross_unsigned_gamma_exposure": gross_gamma if gross_gamma else None,
        "gamma_limitation": "Unsigned gross gamma only; dealer positioning cannot be inferred without a signed-position model.",
    }


def _summarize_fundamentals(value: Mapping[str, Any]) -> dict[str, Any]:
    rows = sorted(_rows(value), key=lambda row: _date(row) or "")
    if not rows:
        return {"status": "insufficient_data", "reason": "No financial periods were supplied."}
    fields = {
        "revenue": ("revenue", "sales"),
        "eps": ("eps", "earnings_per_share"),
        "free_cash_flow": ("free_cash_flow", "fcf"),
        "net_income": ("net_income",),
    }
    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else {}
    metrics: dict[str, Any] = {}
    for label, aliases in fields.items():
        current_value = _number(latest, *aliases)
        previous_value = _number(previous, *aliases)
        metrics[label] = current_value
        metrics[label + "_sequential_growth_pct"] = _percent_change(current_value, previous_value)
    return {
        "status": "calculated",
        "periods": len(rows),
        "latest_period": _date(latest),
        "metrics": metrics,
        "next_earnings_date": value.get("next_earnings_date"),
        "limitation": "Sequential growth is not automatically year-over-year unless the supplied periods are annual or matched quarters.",
    }


def _summarize_news(value: Mapping[str, Any]) -> dict[str, Any]:
    items = _rows(value)
    if not items:
        return {"status": "insufficient_data", "reason": "No timestamped news items were supplied."}
    categories = Counter(str(item.get("category") or "unclassified") for item in items)
    supplied_sentiments = [
        number
        for item in items
        if (number := _number(item, "sentiment")) is not None
    ]
    timestamps = sorted(filter(None, (_date(item) for item in items)))
    return {
        "status": "calculated",
        "item_count": len(items),
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
        "categories": dict(categories),
        "average_supplied_sentiment": (
            sum(supplied_sentiments) / len(supplied_sentiments)
            if supplied_sentiments
            else None
        ),
        "sentiment_limitation": "The adapter does not invent sentiment; it aggregates only supplied numeric values.",
    }


def _summarize_order_book(value: Mapping[str, Any]) -> dict[str, Any]:
    bids = value.get("bids")
    asks = value.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        snapshots = sorted(_rows(value), key=lambda item: _date(item) or "")
        latest = snapshots[-1] if snapshots else {}
        bids = latest.get("bids")
        asks = latest.get("asks")
    def levels(raw: Any) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, Mapping):
                price = _number(item, "price")
                size = _number(item, "size", "quantity")
            elif isinstance(item, list) and len(item) >= 2:
                price = parse_number(item[0])
                size = parse_number(item[1])
            else:
                continue
            if price is not None and size is not None:
                result.append((price, size))
        return result
    bid_levels = sorted(levels(bids), reverse=True)
    ask_levels = sorted(levels(asks))
    if not bid_levels or not ask_levels:
        return {"status": "insufficient_data", "reason": "Both bid and ask depth are required."}
    best_bid = bid_levels[0][0]
    best_ask = ask_levels[0][0]
    mid = (best_bid + best_ask) / 2.0
    bid_depth = sum(size for _, size in bid_levels)
    ask_depth = sum(size for _, size in ask_levels)
    total_depth = bid_depth + ask_depth
    return {
        "status": "calculated",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid,
        "spread": best_ask - best_bid,
        "spread_bps": (best_ask - best_bid) / mid * 10_000.0 if mid else None,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "depth_imbalance": (bid_depth - ask_depth) / total_depth if total_depth else None,
        "levels": {"bids": len(bid_levels), "asks": len(ask_levels)},
        "limitation": "One snapshot measures displayed liquidity only; absorption requires a timestamped sequence of book and trade updates.",
    }


def summarize_context_file(
    path: Path,
    *,
    asset_candles: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved = Path(path).resolve()
    value = json.loads(resolved.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(value, Mapping):
        raise ValueError("Context file must contain a JSON object.")
    context_type = str(value.get("context_type") or value.get("type") or "").lower()
    if context_type not in CONTEXT_TYPES:
        raise ValueError(
            "context_type must be one of: " + ", ".join(CONTEXT_TYPES)
        )
    calculators = {
        "benchmark": lambda: _summarize_benchmark(value, asset_candles),
        "breadth": lambda: _summarize_breadth(value),
        "yields": lambda: _summarize_yields(value),
        "options": lambda: _summarize_options(value),
        "fundamentals": lambda: _summarize_fundamentals(value),
        "news": lambda: _summarize_news(value),
        "order_book": lambda: _summarize_order_book(value),
    }
    summary = calculators[context_type]()
    return {
        "source": str(resolved),
        "context_type": context_type,
        "captured_at": value.get("captured_at"),
        "symbol": value.get("symbol"),
        "summary": summary,
        "policy": (
            "Context may rank a price-valid Elliott count but cannot create pivots, "
            "override hard rules, or become a trade instruction."
        ),
    }
