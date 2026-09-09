from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import calculate_ewo, calculate_macd, calculate_wilder_rsi
from elliott_ai.live_market_data import TwelveDataClient, normalize_market_symbol


TIMEFRAMES = ("monthly", "weekly", "daily", "4h", "1h", "15m")
INTRADAY_DURATION = {
    "4h": timedelta(hours=4),
    "1h": timedelta(hours=1),
    "15m": timedelta(minutes=15),
}
REVERSALS = {
    "monthly": (0.12, 0.20, 0.30),
    "weekly": (0.08, 0.12, 0.20),
    "daily": (0.04, 0.07, 0.12),
    "4h": (0.025, 0.04, 0.07),
    "1h": (0.015, 0.025, 0.04),
    "15m": (0.008, 0.015, 0.025),
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


def current_period_is_complete(
    timestamp: datetime, timeframe: str, captured_at: datetime
) -> bool:
    if timeframe == "monthly":
        local = timestamp.astimezone(NY)
        now_local = captured_at.astimezone(NY)
        return (local.year, local.month) < (now_local.year, now_local.month)
    if timeframe == "weekly":
        local_date = timestamp.astimezone(NY).date()
        now_date = captured_at.astimezone(NY).date()
        week_start = now_date - timedelta(days=now_date.weekday())
        return local_date < week_start
    if timeframe == "daily":
        local_date = timestamp.astimezone(NY).date()
        now_local = captured_at.astimezone(NY)
        if local_date < now_local.date():
            return True
        if local_date > now_local.date():
            return False
        session_close = now_local.replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        return now_local >= session_close

    local = timestamp.astimezone(NY)
    session_close_local = local.replace(hour=16, minute=0, second=0, microsecond=0)
    normal_end = timestamp + INTRADAY_DURATION[timeframe]
    candle_end = min(normal_end, session_close_local.astimezone(timezone.utc))
    return candle_end <= captured_at


def normalize_rows(series: dict[str, Any], captured_at: datetime) -> tuple[list[dict[str, Any]], int]:
    timeframe = str(series["timeframe"])
    intraday = timeframe in INTRADAY_DURATION
    rows: list[dict[str, Any]] = []
    excluded_incomplete = 0
    for source in series["values"]:
        timestamp = parse_timestamp(str(source["datetime"]), intraday=intraday)
        if not current_period_is_complete(timestamp, timeframe, captured_at):
            excluded_incomplete += 1
            continue
        row = {
            "date": timestamp.isoformat(timespec="seconds"),
            "open": float(source["open"]),
            "high": float(source["high"]),
            "low": float(source["low"]),
            "close": float(source["close"]),
            "volume": float(source.get("volume") or 0.0),
        }
        if not all(math.isfinite(row[field]) for field in ("open", "high", "low", "close", "volume")):
            raise ValueError(f"Non-finite {timeframe} OHLCV value at {row['date']}.")
        if row["high"] < max(row["open"], row["close"], row["low"]):
            raise ValueError(f"Invalid {timeframe} high at {row['date']}.")
        if row["low"] > min(row["open"], row["close"], row["high"]):
            raise ValueError(f"Invalid {timeframe} low at {row['date']}.")
        if row["volume"] < 0:
            raise ValueError(f"Negative {timeframe} volume at {row['date']}.")
        rows.append(row)
    rows.sort(key=lambda row: row["date"])
    dates = [row["date"] for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError(f"Duplicate {timeframe} timestamps.")
    if dates != sorted(dates):
        raise ValueError(f"Unordered {timeframe} timestamps.")
    if not rows:
        raise ValueError(f"No completed {timeframe} candles remain.")
    return rows, excluded_incomplete


def enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        row["volume_ratio20"] = row["volume"] / average_volume if average_volume else None
    return rows


@dataclass(frozen=True)
class Pivot:
    index: int
    date: str
    price: float
    kind: str


def zigzag(rows: list[dict[str, Any]], reversal: float) -> list[Pivot]:
    if len(rows) < 2:
        return []
    first = rows[0]
    up_move = rows[1]["high"] / first["low"] - 1.0
    down_move = first["high"] / rows[1]["low"] - 1.0
    direction = 1 if up_move >= down_move else -1
    if direction == 1:
        pivots = [Pivot(0, first["date"], first["low"], "low")]
        extreme_index, extreme_price = 0, first["high"]
    else:
        pivots = [Pivot(0, first["date"], first["high"], "high")]
        extreme_index, extreme_price = 0, first["low"]

    for index, row in enumerate(rows[1:], 1):
        if direction == 1:
            if row["high"] >= extreme_price:
                extreme_index, extreme_price = index, row["high"]
            if row["low"] <= extreme_price * (1.0 - reversal):
                pivots.append(Pivot(extreme_index, rows[extreme_index]["date"], extreme_price, "high"))
                direction = -1
                extreme_index, extreme_price = index, row["low"]
        else:
            if row["low"] <= extreme_price:
                extreme_index, extreme_price = index, row["low"]
            if row["high"] >= extreme_price * (1.0 + reversal):
                pivots.append(Pivot(extreme_index, rows[extreme_index]["date"], extreme_price, "low"))
                direction = 1
                extreme_index, extreme_price = index, row["high"]
    pivots.append(
        Pivot(
            extreme_index,
            rows[extreme_index]["date"],
            extreme_price,
            "high" if direction == 1 else "low",
        )
    )
    deduplicated: list[Pivot] = []
    for pivot in pivots:
        if not deduplicated or (pivot.index, pivot.kind) != (deduplicated[-1].index, deduplicated[-1].kind):
            deduplicated.append(pivot)
    return deduplicated


def pivot_payload(rows: list[dict[str, Any]], pivots: list[Pivot]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for pivot in pivots:
        row = rows[pivot.index]
        result.append(
            {
                "date": pivot.date,
                "price": round(pivot.price, 6),
                "kind": pivot.kind,
                "rsi14": None if row["rsi14"] is None else round(float(row["rsi14"]), 4),
                "ewo": None if row["ewo"] is None else round(float(row["ewo"]), 6),
                "volume": row["volume"],
                "volume_ratio20": None if row["volume_ratio20"] is None else round(float(row["volume_ratio20"]), 4),
            }
        )
    return result


def timeframe_summary(rows: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    pivot_sets = {
        f"{int(round(reversal * 1000)) / 10:g}%": pivot_payload(rows, zigzag(rows, reversal))
        for reversal in REVERSALS[timeframe]
    }
    latest = rows[-1]
    return {
        "first": rows[0]["date"],
        "last": latest["date"],
        "count": len(rows),
        "last_close": latest["close"],
        "last_rsi14": latest["rsi14"],
        "last_ewo": latest["ewo"],
        "last_macd": latest["macd"],
        "last_volume_ratio20": latest["volume_ratio20"],
        "pivot_sets": pivot_sets,
        "recent_bars": rows[-80:],
    }


def main() -> None:
    api_key = str(os.getenv("TWELVE_DATA_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("TWELVE_DATA_API_KEY is missing.")
    captured_at = datetime.now(timezone.utc)
    symbol = normalize_market_symbol("NASDAQ:SSYS")
    output_directory = (
        ROOT
        / "market_scans"
        / "2026-08-25_ssys"
        / f"SSYS_{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_directory.mkdir(parents=True, exist_ok=False)
    client = TwelveDataClient(api_key, maximum_retries=1)
    manifest_files: list[dict[str, Any]] = []
    analysis: dict[str, Any] = {
        "symbol": "NASDAQ:SSYS",
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "policy": {
            "method": "price_first_candidate_pivots_then_soft_indicators",
            "completed_candles_only": True,
            "rsi": "Wilder 14; same timeframe and feed only",
            "ewo": "SMA(5)-SMA(35) of median price",
            "volume": "same timeframe and feed only",
            "verification": "candidate/unproven until lower-timeframe structure passes hard Elliott rules",
        },
        "timeframes": {},
    }

    for timeframe in TIMEFRAMES:
        series = client.fetch_time_series(symbol, timeframe)
        rows, excluded = normalize_rows(series, captured_at)
        rows = enrich(rows)
        metadata = dict(series["metadata"])
        payload: dict[str, Any] = {
            "metadata": {
                "provider": "twelve_data",
                "provider_symbol": metadata.get("symbol", "SSYS"),
                "exchange": metadata.get("exchange", "NASDAQ"),
                "exchange_timezone": metadata.get("exchange_timezone", "America/New_York"),
                "timeframe": timeframe,
                "provider_interval": series["provider_interval"],
                "session": "regular",
                "adjustment": "splits",
                "dividend_adjustment": "none",
                "captured_at": captured_at.isoformat(timespec="seconds"),
                "applicable_cutoff": rows[-1]["date"],
                "completed_candles_only": True,
                "excluded_incomplete_rows": excluded,
            },
            "candles": rows,
        }
        payload["content_hash"] = canonical_hash(payload)
        path = output_directory / f"SSYS_{timeframe}_closed.json"
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        manifest_files.append(
            {
                "timeframe": timeframe,
                "path": path.name,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_hash": payload["content_hash"],
                "count": len(rows),
                "first": rows[0]["date"],
                "last": rows[-1]["date"],
                "excluded_incomplete_rows": excluded,
            }
        )
        analysis["timeframes"][timeframe] = timeframe_summary(rows, timeframe)

    analysis["content_hash"] = canonical_hash(analysis)
    analysis_path = output_directory / "SSYS_price_first_audit.json"
    analysis_path.write_text(json.dumps(analysis, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    manifest: dict[str, Any] = {
        "symbol": "NASDAQ:SSYS",
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "provider": "twelve_data",
        "session": "regular",
        "adjustment": "splits",
        "dividend_adjustment": "none",
        "files": manifest_files,
        "analysis_file": {
            "path": analysis_path.name,
            "size": analysis_path.stat().st_size,
            "sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
            "content_hash": analysis["content_hash"],
        },
    }
    manifest["content_hash"] = canonical_hash(manifest)
    manifest_path = output_directory / "SSYS_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_directory": str(output_directory), "manifest": manifest}, indent=2))


if __name__ == "__main__":
    main()
