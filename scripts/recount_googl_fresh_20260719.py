from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


DATE_FORMATS = ("%a %d %b '%y %H:%M", "%a %d %b '%y", "%a %d %b %Y %H:%M", "%a %d %b %Y")


def parse_date(value: str) -> pd.Timestamp | None:
    for fmt in DATE_FORMATS:
        try:
            return pd.Timestamp(datetime.strptime(value, fmt))
        except ValueError:
            pass
    return None


def parse_number(value: str) -> float:
    cleaned = str(value).replace("\u202f", "").replace("\xa0", "").replace(",", "").strip()
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)([KMBT]?)", cleaned, flags=re.I)
    if not match:
        return math.nan
    scale = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[match.group(2).upper()]
    return float(match.group(1)) * scale


def load_tv(path: Path) -> pd.DataFrame:
    records = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for record in records:
        date = parse_date(str(record.get("date", "")))
        if date is None:
            continue
        row = {"date": date}
        for column in ("open", "high", "low", "close", "volume"):
            row[column] = parse_number(record.get(column, ""))
        if all(math.isfinite(row[column]) for column in ("open", "high", "low", "close")):
            rows.append(row)
    frame = pd.DataFrame(rows).drop_duplicates("date").sort_values("date").set_index("date")
    return frame


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)
    median = (df["high"] + df["low"]) / 2
    df["ewo"] = median.rolling(5).mean() - median.rolling(35).mean()
    df["ewo_pct"] = 100 * df["ewo"] / df["close"]
    df["volume_sma50"] = df["volume"].rolling(50).mean()
    df["relative_volume"] = df["volume"] / df["volume_sma50"]
    return df


@dataclass(frozen=True)
class Pivot:
    date: pd.Timestamp
    price: float
    kind: str


def zigzag(frame: pd.DataFrame, reversal: float) -> list[Pivot]:
    """Log-proportional ZigZag using intrabar highs/lows and confirmed reversals."""
    if frame.empty:
        return []
    first_date = frame.index[0]
    first_price = float(frame.iloc[0]["low"])
    direction = 0
    extreme_date = first_date
    extreme_price = first_price
    pivots: list[Pivot] = [Pivot(first_date, first_price, "low")]

    for date, row in frame.iloc[1:].iterrows():
        high = float(row["high"])
        low = float(row["low"])
        if direction == 0:
            if high >= extreme_price * (1 + reversal):
                direction = 1
                extreme_date, extreme_price = date, high
            elif low <= extreme_price * (1 - reversal):
                pivots[0] = Pivot(date, low, "low")
                extreme_date, extreme_price = date, low
            continue
        if direction == 1:
            if high >= extreme_price:
                extreme_date, extreme_price = date, high
            if low <= extreme_price * (1 - reversal):
                pivots.append(Pivot(extreme_date, extreme_price, "high"))
                direction = -1
                extreme_date, extreme_price = date, low
        else:
            if low <= extreme_price:
                extreme_date, extreme_price = date, low
            if high >= extreme_price * (1 + reversal):
                pivots.append(Pivot(extreme_date, extreme_price, "low"))
                direction = 1
                extreme_date, extreme_price = date, high
    pivots.append(Pivot(extreme_date, extreme_price, "high" if direction >= 0 else "low"))
    return pivots


def segment_metrics(frame: pd.DataFrame, start: str, end: str, direction: str) -> dict[str, float | str | int]:
    segment = frame.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if segment.empty:
        raise ValueError(f"No data for {start} to {end}")
    endpoint = segment.iloc[-1]
    startpoint = segment.iloc[0]
    extreme_rsi = segment["rsi"].max() if direction == "up" else segment["rsi"].min()
    signed_ewo = segment["ewo_pct"].max() if direction == "up" else segment["ewo_pct"].min()
    return {
        "start": start,
        "end": end,
        "bars": int(len(segment)),
        "start_close": round(float(startpoint["close"]), 4),
        "end_close": round(float(endpoint["close"]), 4),
        "return_pct": round(100 * (float(endpoint["close"]) / float(startpoint["close"]) - 1), 2),
        "endpoint_rsi": round(float(endpoint["rsi"]), 2),
        "extreme_rsi": round(float(extreme_rsi), 2),
        "endpoint_relative_volume": round(float(endpoint["relative_volume"]), 2),
        "average_relative_volume": round(float(segment["relative_volume"].mean()), 2),
        "cumulative_volume": round(float(segment["volume"].sum()), 2),
        "extreme_ewo_pct": round(float(signed_ewo), 2),
    }


def print_pivots(name: str, pivots: list[Pivot]) -> None:
    print(f"\n{name} ({len(pivots)} pivots)")
    for pivot in pivots:
        print(f"{pivot.date.date()} {pivot.kind:4s} {pivot.price:10.4f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily", type=Path, default=Path("tv_googl_daily_complete_20260719.json"))
    parser.add_argument("--weekly", type=Path, default=Path("tv_googl_weekly_2004_chunk_20260719.json"))
    parser.add_argument("--monthly", type=Path, default=Path("tv_googl_monthly_complete_20260719.json"))
    parser.add_argument("--output", type=Path, default=Path("googl_fresh_pivot_audit_20260719.json"))
    args = parser.parse_args()

    daily = add_indicators(load_tv(args.daily))
    weekly = add_indicators(load_tv(args.weekly))
    monthly = add_indicators(load_tv(args.monthly))
    print(f"daily {daily.index.min().date()}..{daily.index.max().date()} rows={len(daily)}")
    print(f"weekly {weekly.index.min().date()}..{weekly.index.max().date()} rows={len(weekly)}")
    print(f"monthly {monthly.index.min().date()}..{monthly.index.max().date()} rows={len(monthly)}")

    result = {"ranges": {}, "pivots": {}}
    for name, frame in (("monthly", monthly), ("weekly", weekly), ("daily", daily)):
        result["ranges"][name] = {
            "start": str(frame.index.min().date()),
            "end": str(frame.index.max().date()),
            "rows": len(frame),
        }
    thresholds = {
        "monthly_50pct": (monthly, 0.50),
        "monthly_30pct": (monthly, 0.30),
        "weekly_30pct": (weekly, 0.30),
        "weekly_20pct": (weekly, 0.20),
        "weekly_12pct": (weekly, 0.12),
        "daily_12pct": (daily, 0.12),
        "daily_8pct": (daily, 0.08),
    }
    for name, (frame, threshold) in thresholds.items():
        pivots = zigzag(frame, threshold)
        print_pivots(name, pivots)
        result["pivots"][name] = [
            {"date": str(p.date.date()), "price": round(p.price, 4), "kind": p.kind} for p in pivots
        ]

    candidate_segments = {
        "Cycle_I": ("2004-08-19", "2007-11-07", "up"),
        "Cycle_II": ("2007-11-07", "2008-11-21", "down"),
        "Cycle_III_Primary_1": ("2008-11-21", "2010-01-04", "up"),
        "Cycle_III_Primary_2": ("2010-01-04", "2010-07-06", "down"),
        "Cycle_III_Primary_3": ("2010-07-06", "2020-02-19", "up"),
        "Cycle_III_Primary_4": ("2020-02-19", "2020-03-23", "down"),
        "Cycle_III_Primary_5": ("2020-03-23", "2022-02-02", "up"),
        "Cycle_IV": ("2022-02-02", "2022-11-03", "down"),
        "Cycle_V_Primary_1": ("2022-11-03", "2024-07-10", "up"),
        "Cycle_V_Primary_2": ("2024-07-10", "2025-04-07", "down"),
        "Cycle_V_Primary_3": ("2025-04-07", "2026-05-18", "up"),
        "Cycle_V_Primary_4_active": ("2026-05-18", "2026-07-17", "down"),
        "P1_Intermediate_1": ("2022-11-03", "2023-02-07", "up"),
        "P1_Intermediate_2": ("2023-02-07", "2023-02-24", "down"),
        "P1_Intermediate_3": ("2023-02-24", "2023-10-12", "up"),
        "P1_Intermediate_4": ("2023-10-12", "2023-10-27", "down"),
        "P1_Intermediate_5": ("2023-10-27", "2024-07-10", "up"),
        "P3_Intermediate_1": ("2025-04-07", "2025-06-10", "up"),
        "P3_Intermediate_2": ("2025-06-10", "2025-06-23", "down"),
        "P3_Intermediate_3": ("2025-06-23", "2025-11-25", "up"),
        "P3_Intermediate_4": ("2025-11-25", "2025-12-17", "down"),
        "P3_Intermediate_5": ("2025-12-17", "2026-05-18", "up"),
        "P3I3_Minor_1": ("2025-06-23", "2025-07-24", "up"),
        "P3I3_Minor_2": ("2025-07-24", "2025-08-01", "down"),
        "P3I3_Minor_3": ("2025-08-01", "2025-09-19", "up"),
        "P3I3_Minor_4": ("2025-09-19", "2025-10-10", "down"),
        "P3I3_Minor_5": ("2025-10-10", "2025-11-25", "up"),
    }
    result["candidate_segment_metrics_daily"] = {
        name: segment_metrics(daily, start, end, direction)
        for name, (start, end, direction) in candidate_segments.items()
    }
    print("\nCANDIDATE SEGMENT METRICS (DAILY)")
    for name, metrics in result["candidate_segment_metrics_daily"].items():
        print(name, json.dumps(metrics, sort_keys=True))
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
