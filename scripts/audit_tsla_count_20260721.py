from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import (
    calculate_ewo,
    calculate_macd,
    calculate_wilder_rsi,
)


@dataclass(frozen=True)
class Pivot:
    timestamp: datetime
    price: float
    kind: str


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_snapshot(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for source in payload["bars"]:
        row = dict(source)
        row["timestamp"] = parse_timestamp(row["date"])
        for field in ("open", "high", "low", "close", "volume"):
            row[field] = float(row[field])
        rows.append(row)
    rows.sort(key=lambda row: row["timestamp"])
    return payload["metadata"], rows


def add_indicators(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    closes = [row["close"] for row in rows]
    ewo = calculate_ewo(highs, lows)["values"]
    macd = calculate_macd(closes)["line"]
    rsi = calculate_wilder_rsi(closes)["values"]
    for index, row in enumerate(rows):
        median = (row["high"] + row["low"]) / 2.0
        row["ewo"] = ewo[index]
        row["ewo_pct"] = (
            100.0 * float(ewo[index]) / median if ewo[index] is not None else None
        )
        row["macd"] = macd[index]
        row["rsi"] = rsi[index]
        baseline_rows = rows[max(0, index - 49) : index + 1]
        baseline = sum(item["volume"] for item in baseline_rows) / len(baseline_rows)
        row["relative_volume_50"] = row["volume"] / baseline if baseline else None
    return rows


def select_rows(
    rows: Iterable[dict[str, Any]], start: str, end: str
) -> list[dict[str, Any]]:
    start_at = parse_timestamp(start)
    end_at = parse_timestamp(end)
    return [row for row in rows if start_at <= row["timestamp"] <= end_at]


def zigzag(
    rows: list[dict[str, Any]],
    start: str,
    end: str,
    reversal: float,
    *,
    start_kind: str,
) -> list[Pivot]:
    selected = select_rows(rows, start, end)
    if not selected:
        return []
    first = selected[0]
    extreme_time = first["timestamp"]
    if start_kind == "low":
        direction = 1
        extreme_price = first["high"]
        pivots = [Pivot(first["timestamp"], first["low"], "low")]
    elif start_kind == "high":
        direction = -1
        extreme_price = first["low"]
        pivots = [Pivot(first["timestamp"], first["high"], "high")]
    else:
        raise ValueError("start_kind must be 'low' or 'high'.")

    for row in selected[1:]:
        high = row["high"]
        low = row["low"]
        if direction == 1:
            if high >= extreme_price:
                extreme_time, extreme_price = row["timestamp"], high
            if low <= extreme_price * (1.0 - reversal):
                pivots.append(Pivot(extreme_time, extreme_price, "high"))
                direction = -1
                extreme_time, extreme_price = row["timestamp"], low
        else:
            if low <= extreme_price:
                extreme_time, extreme_price = row["timestamp"], low
            if high >= extreme_price * (1.0 + reversal):
                pivots.append(Pivot(extreme_time, extreme_price, "low"))
                direction = 1
                extreme_time, extreme_price = row["timestamp"], high
    pivots.append(
        Pivot(extreme_time, extreme_price, "high" if direction >= 0 else "low")
    )
    return pivots


def impulse_rules(points: list[float]) -> dict[str, Any]:
    if len(points) != 6:
        raise ValueError("An impulse rule check requires six boundary points.")
    direction = 1.0 if points[-1] > points[0] else -1.0
    lengths = [
        direction * (points[1] - points[0]),
        direction * (points[3] - points[2]),
        direction * (points[5] - points[4]),
    ]
    log_lengths = [
        direction * math.log(points[1] / points[0]),
        direction * math.log(points[3] / points[2]),
        direction * math.log(points[5] / points[4]),
    ]
    no_overlap = (
        points[4] > points[1] if direction > 0 else points[4] < points[1]
    )
    return {
        "arithmetic_lengths": [round(value, 6) for value in lengths],
        "log_lengths": [round(value, 6) for value in log_lengths],
        "wave_3_not_shortest_arithmetic": lengths[1] > min(lengths[0], lengths[2]),
        "wave_3_not_shortest_log": log_lengths[1]
        > min(log_lengths[0], log_lengths[2]),
        "wave_4_no_overlap": no_overlap,
        "overlap_amount": round(
            direction * (points[4] - points[1]),
            6,
        ),
    }


def segment_metrics(
    rows: list[dict[str, Any]], start: str, end: str, direction: str
) -> dict[str, Any]:
    selected = select_rows(rows, start, end)
    if not selected:
        return {"availability": "unavailable", "bars": 0}

    def finite_values(field: str) -> list[float]:
        return [
            float(row[field])
            for row in selected
            if row.get(field) is not None and math.isfinite(float(row[field]))
        ]

    ewo = finite_values("ewo")
    ewo_pct = finite_values("ewo_pct")
    macd = finite_values("macd")
    rsi = finite_values("rsi")
    relative_volume = finite_values("relative_volume_50")
    signed = max if direction == "up" else min
    volume = sum(row["volume"] for row in selected)
    return {
        "availability": "available",
        "bars": len(selected),
        "cumulative_volume": round(volume, 2),
        "average_volume": round(volume / len(selected), 2),
        "average_relative_volume_50": round(
            sum(relative_volume) / len(relative_volume), 6
        ),
        "signed_ewo_peak": round(signed(ewo), 6) if ewo else None,
        "signed_ewo_pct_peak": round(signed(ewo_pct), 6) if ewo_pct else None,
        "signed_macd_peak": round(signed(macd), 6) if macd else None,
        "rsi_extreme": round(signed(rsi), 6) if rsi else None,
        "rsi_at_end": round(rsi[-1], 6) if rsi else None,
    }


def diagonal_geometry(
    timestamps: list[str], prices: list[float]
) -> dict[str, Any]:
    if len(timestamps) != 6 or len(prices) != 6:
        raise ValueError("A diagonal check requires six boundary points.")
    times = [parse_timestamp(value) for value in timestamps]

    def slope(left: int, right: int) -> float:
        days = (times[right] - times[left]).total_seconds() / 86400.0
        return (math.log(prices[right]) - math.log(prices[left])) / days

    upper_slope = slope(1, 3)
    lower_slope = slope(2, 4)
    elapsed_to_five = (times[5] - times[1]).total_seconds() / 86400.0
    projected_upper_at_five = math.exp(
        math.log(prices[1]) + upper_slope * elapsed_to_five
    )
    actionary = [
        prices[1] - prices[0],
        prices[3] - prices[2],
        prices[5] - prices[4],
    ]
    reactions = [prices[1] - prices[2], prices[3] - prices[4]]
    return {
        "upper_log_slope_per_day": round(upper_slope, 9),
        "lower_log_slope_per_day": round(lower_slope, 9),
        "boundaries_converge": lower_slope > upper_slope,
        "projected_upper_at_wave_5_time": round(projected_upper_at_five, 4),
        "wave_5_distance_from_projected_upper_pct": round(
            100.0 * (prices[5] / projected_upper_at_five - 1.0),
            4,
        ),
        "actionary_lengths": [round(value, 4) for value in actionary],
        "reaction_lengths": [round(value, 4) for value in reactions],
        "strictly_contracting_actionary": actionary[0] > actionary[1] > actionary[2],
        "strictly_expanding_actionary": actionary[0] < actionary[1] < actionary[2],
        "strictly_contracting_reactions": reactions[0] > reactions[1],
        "strictly_expanding_reactions": reactions[0] < reactions[1],
    }


def duration_days(start: str, end: str) -> int:
    return (parse_timestamp(end) - parse_timestamp(start)).days


def ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6)


def pivot_dict(pivot: Pivot) -> dict[str, Any]:
    return {
        "timestamp": pivot.timestamp.isoformat(),
        "price": round(pivot.price, 6),
        "kind": pivot.kind,
    }


def build_exploration() -> dict[str, Any]:
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for name in ("monthly", "weekly", "daily", "4h", "1h", "15m"):
        metadata, rows = load_snapshot(ROOT / f"tv_tsla_{name}_20260721.json")
        snapshots[name] = (metadata, add_indicators(rows))

    ranges = {
        name: {
            "bars": len(rows),
            "start": rows[0]["timestamp"].isoformat(),
            "end": rows[-1]["timestamp"].isoformat(),
            "metadata": metadata,
        }
        for name, (metadata, rows) in snapshots.items()
    }

    intervals = {
        "listed_history": ("2010-06-29T00:00:00+00:00", "2026-07-21T23:59:59+00:00", "low"),
        "cycle_ii": ("2014-09-04T00:00:00+00:00", "2016-02-09T23:59:59+00:00", "high"),
        "primary_3": ("2019-06-03T00:00:00+00:00", "2021-11-04T23:59:59+00:00", "low"),
        "primary_4": ("2021-11-04T00:00:00+00:00", "2023-01-06T23:59:59+00:00", "high"),
        "diagonal_parent": ("2023-01-06T00:00:00+00:00", "2025-12-22T23:59:59+00:00", "low"),
        "diagonal_wave_1": ("2023-01-06T00:00:00+00:00", "2023-07-19T23:59:59+00:00", "low"),
        "diagonal_wave_2": ("2023-07-19T00:00:00+00:00", "2024-04-22T23:59:59+00:00", "high"),
        "diagonal_wave_3": ("2024-04-22T00:00:00+00:00", "2024-12-18T23:59:59+00:00", "low"),
        "diagonal_wave_4": ("2024-12-18T00:00:00+00:00", "2025-04-07T23:59:59+00:00", "high"),
        "diagonal_wave_5": ("2025-04-07T00:00:00+00:00", "2025-12-22T23:59:59+00:00", "low"),
        "active_cycle_iv": ("2025-12-22T00:00:00+00:00", "2026-07-21T23:59:59+00:00", "high"),
        "active_primary_w": ("2025-12-22T00:00:00+00:00", "2026-04-07T23:59:59+00:00", "high"),
        "active_primary_x": ("2026-04-07T00:00:00+00:00", "2026-05-13T23:59:59+00:00", "low"),
        "active_primary_y": ("2026-05-13T00:00:00+00:00", "2026-07-21T23:59:59+00:00", "high"),
    }
    thresholds = {
        "listed_history": (0.18, 0.25, 0.35),
        "cycle_ii": (0.08, 0.12, 0.18),
        "primary_3": (0.08, 0.12, 0.18),
        "primary_4": (0.06, 0.10, 0.14),
        "diagonal_parent": (0.08, 0.12, 0.18),
        "diagonal_wave_1": (0.04, 0.06, 0.09),
        "diagonal_wave_2": (0.04, 0.06, 0.09),
        "diagonal_wave_3": (0.04, 0.06, 0.09),
        "diagonal_wave_4": (0.04, 0.06, 0.09),
        "diagonal_wave_5": (0.04, 0.06, 0.09),
        "active_cycle_iv": (0.04, 0.06, 0.09),
        "active_primary_w": (0.03, 0.05, 0.08),
        "active_primary_x": (0.03, 0.05, 0.08),
        "active_primary_y": (0.03, 0.05, 0.08),
    }
    timeframe_for_interval = {
        "listed_history": "weekly",
        "cycle_ii": "daily",
        "primary_3": "daily",
        "primary_4": "daily",
        "diagonal_parent": "daily",
        "diagonal_wave_1": "4h",
        "diagonal_wave_2": "4h",
        "diagonal_wave_3": "4h",
        "diagonal_wave_4": "4h",
        "diagonal_wave_5": "4h",
        "active_cycle_iv": "daily",
        "active_primary_w": "4h",
        "active_primary_x": "4h",
        "active_primary_y": "1h",
    }
    ladders: dict[str, Any] = {}
    for name, (start, end, start_kind) in intervals.items():
        timeframe = timeframe_for_interval[name]
        rows = snapshots[timeframe][1]
        ladders[name] = {
            "timeframe": timeframe,
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        rows,
                        start,
                        end,
                        threshold,
                        start_kind=start_kind,
                    )
                ]
                for threshold in thresholds[name]
            },
        }

    diagonal_times = [
        "2023-01-06T14:30:00+00:00",
        "2023-07-19T13:30:00+00:00",
        "2024-04-22T13:30:00+00:00",
        "2024-12-18T14:30:00+00:00",
        "2025-04-07T13:30:00+00:00",
        "2025-12-22T14:30:00+00:00",
    ]
    diagonal_prices = [101.81, 299.29, 138.8025, 488.5399, 214.25, 498.83]
    daily = snapshots["daily"][1]
    diagonal_metrics = {
        "wave_1": segment_metrics(
            daily, diagonal_times[0], diagonal_times[1], "up"
        ),
        "wave_3": segment_metrics(
            daily, diagonal_times[2], diagonal_times[3], "up"
        ),
        "wave_5": segment_metrics(
            daily, diagonal_times[4], diagonal_times[5], "up"
        ),
    }
    monthly = snapshots["monthly"][1]
    listed_history_metrics = {
        "wave_1": segment_metrics(
            monthly,
            "2010-07-01T00:00:00+00:00",
            "2014-09-30T23:59:59+00:00",
            "up",
        ),
        "wave_3": segment_metrics(
            monthly,
            "2016-02-01T00:00:00+00:00",
            "2021-11-30T23:59:59+00:00",
            "up",
        ),
        "wave_5": segment_metrics(
            monthly,
            "2023-01-01T00:00:00+00:00",
            "2025-12-31T23:59:59+00:00",
            "up",
        ),
    }
    nested_cycle_iii_metrics = {
        "primary_1": segment_metrics(
            monthly,
            "2016-02-01T00:00:00+00:00",
            "2017-09-30T23:59:59+00:00",
            "up",
        ),
        "primary_3": segment_metrics(
            monthly,
            "2019-06-01T00:00:00+00:00",
            "2021-11-30T23:59:59+00:00",
            "up",
        ),
        "primary_5": segment_metrics(
            monthly,
            "2023-01-01T00:00:00+00:00",
            "2025-12-31T23:59:59+00:00",
            "up",
        ),
    }
    fifteen_minute = snapshots["15m"][1]
    one_hour = snapshots["1h"][1]
    latest_decline_metrics = {
        "wave_1": segment_metrics(
            fifteen_minute,
            "2026-07-06T19:45:00+00:00",
            "2026-07-08T18:45:00+00:00",
            "down",
        ),
        "wave_3": segment_metrics(
            fifteen_minute,
            "2026-07-10T16:30:00+00:00",
            "2026-07-17T13:30:00+00:00",
            "down",
        ),
        "wave_5": segment_metrics(
            fifteen_minute,
            "2026-07-20T13:30:00+00:00",
            "2026-07-20T19:45:00+00:00",
            "down",
        ),
    }
    latest_decline_metrics_1h = {
        "wave_1": segment_metrics(
            one_hour,
            "2026-07-06T19:30:00+00:00",
            "2026-07-08T18:30:00+00:00",
            "down",
        ),
        "wave_3": segment_metrics(
            one_hour,
            "2026-07-10T16:30:00+00:00",
            "2026-07-17T13:30:00+00:00",
            "down",
        ),
        "wave_5": segment_metrics(
            one_hour,
            "2026-07-20T13:30:00+00:00",
            "2026-07-20T19:30:00+00:00",
            "down",
        ),
    }

    return {
        "audit_id": "tsla-count-exploration-20260721-v1",
        "source_cutoff": "2026-07-21T19:45:00+00:00",
        "ranges": ranges,
        "pivot_ladders": ladders,
        "hard_rule_checks": {
            "completed_listed_history_parent": impulse_rules(
                [0.9987, 19.4280, 9.4033, 414.4963, 101.81, 498.83]
            ),
            "cycle_i": impulse_rules(
                [0.9987, 2.4280, 1.4073, 12.9667, 7.7400, 19.4280]
            ),
            "cycle_iii": impulse_rules(
                [9.4033, 25.9740, 11.7994, 414.4963, 101.81, 498.83]
            ),
            "degree_shift_cycle_iii_2016_2021": impulse_rules(
                [9.4033, 25.9740, 11.7994, 300.1330, 179.8298, 414.4963]
            ),
            "primary_3": impulse_rules(
                [11.7994, 64.5993, 23.3673, 300.1330, 179.8298, 414.4963]
            ),
            "diagonal_as_normal_impulse": impulse_rules(diagonal_prices),
            "cycle_ii_c_expanding_diagonal_candidate": impulse_rules(
                [19.11, 15.516, 17.397, 13.0, 18.105, 9.4033]
            ),
            "primary_4_a_leading_diagonal_candidate": impulse_rules(
                [414.4963, 295.373, 402.666, 233.333, 384.29, 206.8565]
            ),
            "primary_4_c_impulse": impulse_rules(
                [314.6664, 265.74, 313.8, 166.185, 198.92, 101.81]
            ),
            "diagonal_wave_1_a_impulse": impulse_rules(
                [101.81, 136.65, 124.31, 180.67, 162.79, 217.65]
            ),
            "diagonal_wave_1_c_impulse": impulse_rules(
                [152.37, 177.38, 164.35, 192.96, 178.22, 299.29]
            ),
            "terminal_c_inside_diagonal_wave_5": impulse_rules(
                [273.21, 357.54, 288.7701, 470.76, 382.78, 498.83]
            ),
            "latest_five_down": impulse_rules(
                [419.99, 390.52, 413.15, 377.23, 386.52, 369.435]
            ),
            "initial_498_to_337_decline_as_impulse": impulse_rules(
                [498.83, 387.531, 436.35, 381.4, 416.38, 337.24]
            ),
        },
        "diagonal_geometry": diagonal_geometry(diagonal_times, diagonal_prices),
        "diagonal_motive_metrics_daily": diagonal_metrics,
        "high_degree_metrics_monthly": {
            "completed_listed_history_candidate": listed_history_metrics,
            "nested_cycle_iii_candidate": nested_cycle_iii_metrics,
        },
        "latest_decline_metrics_15m": latest_decline_metrics,
        "latest_decline_metrics_1h": latest_decline_metrics_1h,
        "durations_days": {
            "completed_listed_history_parent": duration_days(
                "2010-07-07T13:30:00+00:00",
                "2025-12-22T14:30:00+00:00",
            ),
            "listed_wave_1": duration_days(
                "2010-07-07T13:30:00+00:00",
                "2014-09-04T13:30:00+00:00",
            ),
            "listed_wave_2": duration_days(
                "2014-09-04T13:30:00+00:00",
                "2016-02-09T14:30:00+00:00",
            ),
            "listed_wave_3": duration_days(
                "2016-02-09T14:30:00+00:00",
                "2021-11-04T13:30:00+00:00",
            ),
            "listed_wave_4": duration_days(
                "2021-11-04T13:30:00+00:00",
                "2023-01-06T14:30:00+00:00",
            ),
            "listed_wave_5": duration_days(
                "2023-01-06T14:30:00+00:00",
                "2025-12-22T14:30:00+00:00",
            ),
            "nested_cycle_iii": duration_days(
                "2016-02-09T14:30:00+00:00",
                "2025-12-22T14:30:00+00:00",
            ),
        },
        "fibonacci_relationships": {
            "cycle_ii_flat_candidate": {
                "b_retracement_of_a": ratio(
                    19.11 - 12.0933,
                    19.428 - 12.0933,
                ),
                "c_to_a": ratio(
                    19.11 - 9.4033,
                    19.428 - 12.0933,
                ),
            },
            "primary_4_zigzag_candidate": {
                "b_retracement_of_a": ratio(
                    314.6664 - 206.8565,
                    414.4963 - 206.8565,
                ),
                "c_to_a": ratio(
                    314.6664 - 101.81,
                    414.4963 - 206.8565,
                ),
            },
            "ending_diagonal_candidate": {
                "wave_2_retracement_of_wave_1": ratio(
                    299.29 - 138.8025,
                    299.29 - 101.81,
                ),
                "wave_3_to_wave_1": ratio(
                    488.5399 - 138.8025,
                    299.29 - 101.81,
                ),
                "wave_4_retracement_of_wave_3": ratio(
                    488.5399 - 214.25,
                    488.5399 - 138.8025,
                ),
                "wave_5_to_wave_3": ratio(
                    498.83 - 214.25,
                    488.5399 - 138.8025,
                ),
                "wave_5_beyond_wave_3_pct": round(
                    100.0 * (498.83 / 488.5399 - 1.0),
                    6,
                ),
            },
            "active_outer_wxy_candidate": {
                "x_retracement_of_w": ratio(
                    453.4 - 337.24,
                    498.83 - 337.24,
                ),
                "y_to_w_at_cutoff": ratio(
                    453.4 - 369.435,
                    498.83 - 337.24,
                ),
                "w_equality_target_from_x": round(
                    453.4 - (498.83 - 337.24),
                    4,
                ),
                "w_0_618_target_from_x": round(
                    453.4 - 0.618 * (498.83 - 337.24),
                    4,
                ),
            },
            "active_y_internal_wxy_candidate": {
                "x_retracement_of_w": ratio(
                    432.8599 - 368.6,
                    453.4 - 368.6,
                ),
                "y_to_w_at_cutoff": ratio(
                    432.8599 - 369.435,
                    453.4 - 368.6,
                ),
                "y_equality_target": round(
                    432.8599 - (453.4 - 368.6),
                    4,
                ),
            },
            "latest_five_down": {
                "wave_3_to_wave_1": ratio(
                    413.15 - 377.23,
                    419.99 - 390.52,
                ),
                "wave_5_to_wave_1": ratio(
                    386.52 - 369.435,
                    419.99 - 390.52,
                ),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "tsla_count_validation_exploration_20260721.json",
    )
    args = parser.parse_args()
    result = build_exploration()
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
