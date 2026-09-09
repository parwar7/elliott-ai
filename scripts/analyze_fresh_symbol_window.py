"""Fetch and measure a fresh, count-free multi-timeframe market window.

This utility deliberately performs no Elliott labeling. It acquires one
regular-session Twelve Data feed, keeps completed candles only, calculates the
repository's canonical indicators, and emits price-first pivot candidates for
manual structural analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import calculate_ewo, calculate_macd, calculate_wilder_rsi
from elliott_ai.live_market_data import TwelveDataClient, normalize_market_symbol
from scripts.analyze_ssys_fresh_20260825 import REVERSALS, pivot_payload, zigzag


TIMEFRAMES = ("monthly", "weekly", "daily", "4h", "1h", "15m")
INTRADAY_DURATION = {
    "4h": timedelta(hours=4),
    "1h": timedelta(hours=1),
    "15m": timedelta(minutes=15),
}
NY = ZoneInfo("America/New_York")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_timestamp(value: str, *, intraday: bool) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc if intraday else NY)
    return parsed.astimezone(timezone.utc)


def _current_week_complete(captured_at: datetime) -> bool:
    local = captured_at.astimezone(NY)
    friday = local.date() + timedelta(days=4 - local.weekday())
    close = datetime.combine(friday, time(16, 0), NY)
    return local >= close


def candle_is_complete(timestamp: datetime, timeframe: str, captured_at: datetime) -> bool:
    local = timestamp.astimezone(NY)
    current_local = captured_at.astimezone(NY)
    if timeframe == "monthly":
        return (local.year, local.month) < (current_local.year, current_local.month)
    if timeframe == "weekly":
        row_week = local.date() - timedelta(days=local.weekday())
        current_week = current_local.date() - timedelta(days=current_local.weekday())
        return row_week < current_week or (
            row_week == current_week and _current_week_complete(captured_at)
        )
    if timeframe == "daily":
        if local.date() < current_local.date():
            return True
        if local.date() > current_local.date():
            return False
        return current_local.time() >= time(16, 0)

    session_open = time(9, 30)
    session_close = time(16, 0)
    if not session_open <= local.time() < session_close:
        return False
    close_local = datetime.combine(local.date(), session_close, NY)
    nominal_end = timestamp + INTRADAY_DURATION[timeframe]
    actual_end = min(nominal_end, close_local.astimezone(timezone.utc))
    return actual_end <= captured_at


def normalize_rows(
    series: dict[str, Any], captured_at: datetime
) -> tuple[list[dict[str, Any]], int]:
    timeframe = str(series["timeframe"])
    intraday = timeframe in INTRADAY_DURATION
    rows: list[dict[str, Any]] = []
    excluded = 0
    for source in series["values"]:
        timestamp = parse_timestamp(str(source["datetime"]), intraday=intraday)
        if not candle_is_complete(timestamp, timeframe, captured_at):
            excluded += 1
            continue
        row = {
            "date": timestamp.isoformat(timespec="seconds"),
            "open": float(source["open"]),
            "high": float(source["high"]),
            "low": float(source["low"]),
            "close": float(source["close"]),
            "volume": float(source.get("volume") or 0.0),
        }
        values = (row["open"], row["high"], row["low"], row["close"], row["volume"])
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"Non-finite {timeframe} OHLCV value at {row['date']}.")
        if row["high"] < max(row["open"], row["close"], row["low"]):
            raise ValueError(f"Invalid {timeframe} high at {row['date']}.")
        if row["low"] > min(row["open"], row["close"], row["high"]):
            raise ValueError(f"Invalid {timeframe} low at {row['date']}.")
        if row["volume"] < 0:
            raise ValueError(f"Negative {timeframe} volume at {row['date']}.")
        rows.append(row)
    rows.sort(key=lambda item: item["date"])
    timestamps = [row["date"] for row in rows]
    if not rows:
        raise ValueError(f"No completed {timeframe} candles remain.")
    if len(timestamps) != len(set(timestamps)):
        raise ValueError(f"Duplicate {timeframe} timestamps.")
    return rows, excluded


def enrich(rows: list[dict[str, Any]]) -> None:
    closes = [row["close"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    rsi = calculate_wilder_rsi(closes)["values"]
    ewo = calculate_ewo(highs, lows)["values"]
    macd = calculate_macd(closes)
    for index, row in enumerate(rows):
        history = rows[max(0, index - 19) : index + 1]
        average_volume = sum(item["volume"] for item in history) / len(history)
        row["rsi14"] = rsi[index]
        row["ewo"] = ewo[index]
        row["macd"] = macd["line"][index]
        row["macd_signal"] = macd["signal"][index]
        row["volume_average20"] = average_volume
        row["volume_ratio20"] = row["volume"] / average_volume if average_volume else None


def scoped_rows(rows: list[dict[str, Any]], timeframe: str, start: date) -> list[dict[str, Any]]:
    if timeframe == "monthly":
        return [
            row
            for row in rows
            if (datetime.fromisoformat(row["date"]).astimezone(NY).year,
                datetime.fromisoformat(row["date"]).astimezone(NY).month)
            >= (start.year, start.month)
        ]
    if timeframe == "weekly":
        week_start = start - timedelta(days=start.weekday())
        return [
            row
            for row in rows
            if datetime.fromisoformat(row["date"]).astimezone(NY).date() >= week_start
        ]
    return [
        row
        for row in rows
        if datetime.fromisoformat(row["date"]).astimezone(NY).date() >= start
    ]


def summary(rows: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    latest = rows[-1]
    pivots = {
        f"{reversal * 100:g}%": pivot_payload(rows, zigzag(rows, reversal))
        for reversal in REVERSALS[timeframe]
    }
    return {
        "first": rows[0]["date"],
        "last": latest["date"],
        "count": len(rows),
        "last_close": latest["close"],
        "last_rsi14": latest["rsi14"],
        "last_ewo": latest["ewo"],
        "last_macd": latest["macd"],
        "last_volume_ratio20": latest["volume_ratio20"],
        "pivot_sets": pivots,
        "bars": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output-root", default=str(ROOT / "market_scans"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = str(os.getenv("TWELVE_DATA_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("TWELVE_DATA_API_KEY is missing.")
    start = date.fromisoformat(args.start)
    captured_at = datetime.now(timezone.utc)
    symbol = normalize_market_symbol(args.symbol)
    slug = re.sub(r"[^A-Z0-9]+", "_", symbol.canonical_symbol).strip("_")
    output_directory = (
        Path(args.output_root).resolve()
        / f"{captured_at.date().isoformat()}_{symbol.provider_symbol.lower()}_fresh"
        / f"{slug}_{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_directory.mkdir(parents=True, exist_ok=False)
    client = TwelveDataClient(api_key, maximum_retries=1)

    audit: dict[str, Any] = {
        "symbol": symbol.canonical_symbol,
        "start_date": start.isoformat(),
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "policy": {
            "method": "fresh price-first pivots; indicators are soft evidence only",
            "provider": "twelve_data",
            "session": "regular",
            "adjustment": "splits",
            "dividend_adjustment": "none",
            "completed_candles_only": True,
            "rsi": "canonical Wilder RSI(14)",
            "ewo": "SMA(5)-SMA(35) of median price",
            "verification": "no Elliott label is generated or verified by this utility",
        },
        "timeframes": {},
    }
    files: list[dict[str, Any]] = []
    for timeframe in TIMEFRAMES:
        series = client.fetch_time_series(symbol, timeframe)
        all_rows, excluded = normalize_rows(series, captured_at)
        enrich(all_rows)
        window = scoped_rows(all_rows, timeframe, start)
        if not window:
            raise ValueError(f"No {timeframe} rows cover the requested window.")
        metadata = dict(series["metadata"])
        payload: dict[str, Any] = {
            "metadata": {
                "provider": "twelve_data",
                "provider_symbol": metadata.get("symbol", symbol.provider_symbol),
                "exchange": metadata.get("exchange", symbol.exchange or "unknown"),
                "mic_code": metadata.get("mic_code"),
                "exchange_timezone": metadata.get("exchange_timezone", "America/New_York"),
                "timeframe": timeframe,
                "provider_interval": series["provider_interval"],
                "session": "regular",
                "adjustment": "splits",
                "dividend_adjustment": "none",
                "captured_at": captured_at.isoformat(timespec="seconds"),
                "applicable_cutoff": window[-1]["date"],
                "completed_candles_only": True,
                "excluded_incomplete_or_nonregular_rows": excluded,
            },
            "candles": window,
        }
        payload["content_hash"] = canonical_hash(payload)
        path = output_directory / f"{symbol.provider_symbol}_{timeframe}_closed.json"
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        files.append(
            {
                "timeframe": timeframe,
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_hash": payload["content_hash"],
                "count": len(window),
                "first": window[0]["date"],
                "last": window[-1]["date"],
                "excluded": excluded,
            }
        )
        audit["timeframes"][timeframe] = summary(window, timeframe)

    audit["content_hash"] = canonical_hash(audit)
    audit_path = output_directory / f"{symbol.provider_symbol}_price_first_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    manifest: dict[str, Any] = {
        "symbol": symbol.canonical_symbol,
        "start_date": start.isoformat(),
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "files": files,
        "audit": {
            "path": audit_path.name,
            "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "content_hash": audit["content_hash"],
        },
    }
    manifest["content_hash"] = canonical_hash(manifest)
    manifest_path = output_directory / f"{symbol.provider_symbol}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_directory": str(output_directory), "manifest": manifest}, indent=2))


if __name__ == "__main__":
    main()
