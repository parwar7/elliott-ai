from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_msft_cycle_iv_rsi_volume_20260725 import (
    Boundary,
    add_rsi_and_volume,
    motive_evidence,
    parse_timestamp,
    segment_metrics,
)
from scripts.audit_tsla_count_20260721 import impulse_rules


SNAPSHOT_DATE = "20260726"
TIMEFRAMES = ("weekly", "daily", "4h", "1h", "30m", "15m")

INTERMEDIATE_3 = [
    Boundary("2024-12-19T14:30:00+00:00", 0.7210),
    Boundary("2025-02-19T14:30:00+00:00", 2.5500),
    Boundary("2025-03-31T13:30:00+00:00", 1.5700),
    Boundary("2025-06-25T13:30:00+00:00", 8.1150),
    Boundary("2025-08-20T13:30:00+00:00", 4.3716),
    Boundary("2025-10-13T13:30:00+00:00", 11.3500),
]

INTERMEDIATE_4_WXY = [
    INTERMEDIATE_3[-1],
    Boundary("2025-11-17T14:30:00+00:00", 4.7500),
    Boundary("2026-01-22T14:30:00+00:00", 10.0500),
    Boundary("2026-07-17T13:30:00+00:00", 3.5700),
]

Y_DOUBLE_ZIGZAG = [
    INTERMEDIATE_4_WXY[2],
    Boundary("2026-03-30T13:30:00+00:00", 3.9300),
    Boundary("2026-06-02T14:30:00+00:00", 6.6400),
    INTERMEDIATE_4_WXY[3],
]

FINAL_Y_ZIGZAG = [
    Y_DOUBLE_ZIGZAG[2],
    Boundary("2026-06-09T16:30:00+00:00", 4.7850),
    Boundary("2026-06-12T14:30:00+00:00", 5.8150),
    Y_DOUBLE_ZIGZAG[3],
]

FINAL_Y_WAVE_A = [
    FINAL_Y_ZIGZAG[0],
    Boundary("2026-06-03T16:30:00+00:00", 6.0400),
    Boundary("2026-06-03T19:30:00+00:00", 6.2800),
    Boundary("2026-06-05T18:30:00+00:00", 5.0800),
    Boundary("2026-06-08T13:30:00+00:00", 5.2800),
    FINAL_Y_ZIGZAG[1],
]

FINAL_Y_WAVE_C = [
    FINAL_Y_ZIGZAG[2],
    Boundary("2026-06-18T16:30:00+00:00", 5.0000),
    Boundary("2026-06-22T13:30:00+00:00", 5.2150),
    Boundary("2026-07-08T14:30:00+00:00", 3.8450),
    Boundary("2026-07-10T16:30:00+00:00", 4.2950),
    FINAL_Y_ZIGZAG[3],
]

REVERSAL_WAVE_1 = [
    FINAL_Y_ZIGZAG[3],
    Boundary("2026-07-17T17:15:00+00:00", 3.8250),
    Boundary("2026-07-20T14:15:00+00:00", 3.6300),
    Boundary("2026-07-21T15:15:00+00:00", 4.0150),
    Boundary("2026-07-21T16:30:00+00:00", 3.9700),
    Boundary("2026-07-21T17:45:00+00:00", 4.0600),
]


def load_snapshot(
    timeframe: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(
        (ROOT / f"tv_tmc_{timeframe}_{SNAPSHOT_DATE}.json").read_text(
            encoding="utf-8"
        )
    )
    if payload["metadata"].get("resolved_symbol") != "BATS:TMC":
        raise ValueError(
            f"{timeframe} resolved to {payload['metadata'].get('resolved_symbol')}"
        )
    rows: list[dict[str, Any]] = []
    for source in payload["bars"]:
        row = dict(source)
        row["timestamp"] = parse_timestamp(row["date"])
        for field in ("open", "high", "low", "close", "volume"):
            row[field] = float(row[field])
        rows.append(row)
    rows.sort(key=lambda row: row["timestamp"])
    return payload["metadata"], add_rsi_and_volume(rows)


def sequence_metrics(
    boundaries: list[Boundary], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        segment_metrics(rows, start, end)
        for start, end in zip(boundaries[:-1], boundaries[1:])
    ]


def motive_by_timeframe(
    boundaries: list[Boundary],
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for timeframe in TIMEFRAMES:
        rows = snapshots[timeframe][1]
        if (
            parse_timestamp(boundaries[0].timestamp) < rows[0]["timestamp"]
            or parse_timestamp(boundaries[-1].timestamp) > rows[-1]["timestamp"]
        ):
            result[timeframe] = {"availability": "unavailable"}
            continue
        waves = sequence_metrics(boundaries, rows)
        if any(wave.get("availability") != "available" for wave in waves):
            result[timeframe] = {
                "availability": "unavailable",
                "reason": "Timeframe is too coarse for every internal segment.",
            }
            continue
        evidence = motive_evidence(waves)
        result[timeframe] = {
            "availability": "available",
            "wave_1_rsi_extreme": waves[0]["rsi_extreme_in_wave_direction"],
            "wave_3_rsi_extreme": waves[2]["rsi_extreme_in_wave_direction"],
            "wave_3_rsi_end": waves[2]["rsi_at_end_bar_close"],
            "wave_5_rsi_extreme": waves[4]["rsi_extreme_in_wave_direction"],
            "wave_5_rsi_end": waves[4]["rsi_at_end_bar_close"],
            "wave_3_to_wave_1_relative_volume_ratio": evidence[
                "wave_3_average_relative_volume_vs_wave_1"
            ],
            "wave_5_to_wave_3_relative_volume_ratio": evidence[
                "wave_5_relative_volume_ratio_to_wave_3"
            ],
            "wave_5_endpoint_rsi_divergence": evidence[
                "wave_5_aligned_endpoint_rsi_divergence_vs_wave_3"
            ],
            "wave_2_volume_dry_up": evidence["wave_2_volume_dry_up_vs_wave_1"],
            "wave_4_volume_dry_up": evidence["wave_4_volume_dry_up_vs_wave_3"],
        }
    return result


def pivot_context(
    rows: list[dict[str, Any]], timestamp: str, price: float
) -> dict[str, Any]:
    target = parse_timestamp(timestamp)
    candidates = [
        row
        for row in rows
        if abs((row["timestamp"] - target).total_seconds()) <= 60 * 60 * 24
    ]
    row = min(
        candidates,
        key=lambda item: (abs(item["low"] - price), abs((item["timestamp"] - target).total_seconds())),
    )
    return {
        "timestamp": row["timestamp"].isoformat(),
        "low": row["low"],
        "close": row["close"],
        "rsi": round(float(row["rsi"]), 6) if row["rsi"] is not None else None,
        "relative_volume_50": round(float(row["relative_volume_50"]), 6),
        "raw_volume": row["volume"],
    }


def ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6)


def build_audit() -> dict[str, Any]:
    snapshots = {timeframe: load_snapshot(timeframe) for timeframe in TIMEFRAMES}
    int3_points = [item.price for item in INTERMEDIATE_3]
    final_a_points = [item.price for item in FINAL_Y_WAVE_A]
    final_c_points = [item.price for item in FINAL_Y_WAVE_C]
    reversal_wave_1_points = [item.price for item in REVERSAL_WAVE_1]

    int3_start, int3_end = INTERMEDIATE_3[0].price, INTERMEDIATE_3[-1].price
    int4_low = INTERMEDIATE_4_WXY[-1].price
    w_length = INTERMEDIATE_4_WXY[0].price - INTERMEDIATE_4_WXY[1].price
    y_length = INTERMEDIATE_4_WXY[2].price - INTERMEDIATE_4_WXY[3].price
    final_a_length = FINAL_Y_ZIGZAG[0].price - FINAL_Y_ZIGZAG[1].price
    final_c_length = FINAL_Y_ZIGZAG[2].price - FINAL_Y_ZIGZAG[3].price
    current_price = snapshots["daily"][1][-1]["close"]
    reversal_advance = REVERSAL_WAVE_1[-1].price - REVERSAL_WAVE_1[0].price

    divergence: dict[str, Any] = {}
    for timeframe in ("daily", "4h", "1h", "30m", "15m"):
        rows = snapshots[timeframe][1]
        march = pivot_context(rows, Y_DOUBLE_ZIGZAG[1].timestamp, 3.93)
        july = pivot_context(rows, Y_DOUBLE_ZIGZAG[3].timestamp, 3.57)
        divergence[timeframe] = {
            "march_low": march,
            "july_low": july,
            "aligned_bullish_rsi_divergence": (
                july["low"] < march["low"] and july["rsi"] > march["rsi"]
            ),
            "july_relative_volume_vs_march": ratio(
                july["relative_volume_50"], march["relative_volume_50"]
            ),
        }

    sources = {
        timeframe: {
            key: snapshots[timeframe][0].get(key)
            for key in (
                "resolved_symbol",
                "exchange",
                "listed_exchange",
                "resolution",
                "session",
                "exchange_timezone",
                "captured_at",
            )
        }
        for timeframe in TIMEFRAMES
    }

    return {
        "analysis_id": "tmc-fresh-count-20260726-v1",
        "symbol": "NASDAQ:TMC",
        "data_cutoff": snapshots["daily"][1][-1]["timestamp"].isoformat(),
        "method": {
            "hard_rules": "Elliott price structure",
            "confirmation": "canonical Wilder RSI(14) and same-feed relative volume",
            "fibonacci": "arithmetic scale for the active correction",
            "excluded": ["EWO", "MACD", "outcome-aware ranking"],
        },
        "sources": sources,
        "preferred_hierarchy": {
            "Primary_I": {
                "status": "active",
                "origin": ["2022-12-19", 0.522],
            },
            "Intermediate_1": {
                "start": ["2022-12-19", 0.522],
                "end": ["2023-07-10", 3.20],
                "status": "complete",
            },
            "Intermediate_2": {
                "start": ["2023-07-10", 3.20],
                "end": ["2024-12-19", 0.721],
                "status": "complete complex correction",
            },
            "Intermediate_3": {
                "start": ["2024-12-19", 0.721],
                "end": ["2025-10-13", 11.35],
                "status": "complete impulse",
            },
            "Intermediate_4": {
                "start": ["2025-10-13", 11.35],
                "working_end": ["2026-07-17", 3.57],
                "structure": "W-X-Y complex correction",
                "status": "price-complete candidate; reversal unconfirmed",
                "w": ["2025-11-17", 4.75],
                "x": ["2026-01-22", 10.05],
                "y": ["2026-07-17", 3.57],
            },
            "Intermediate_5": {
                "status": "not confirmed started",
            },
        },
        "price_validation": {
            "Intermediate_3": impulse_rules(int3_points),
            "final_Y_wave_A": impulse_rules(final_a_points),
            "final_Y_wave_C": impulse_rules(final_c_points),
            "reversal_wave_1_candidate": impulse_rules(reversal_wave_1_points),
            "Intermediate_4_fibonacci": {
                "retracement_of_Intermediate_3": ratio(
                    int3_end - int4_low, int3_end - int3_start
                ),
                "W_length": round(w_length, 6),
                "X_retracement_of_W": ratio(
                    INTERMEDIATE_4_WXY[2].price - INTERMEDIATE_4_WXY[1].price,
                    w_length,
                ),
                "Y_length": round(y_length, 6),
                "Y_to_W": ratio(y_length, w_length),
                "W_equals_Y_target": round(
                    INTERMEDIATE_4_WXY[2].price - w_length, 6
                ),
                "current_low_distance_from_equality": round(
                    int4_low - (INTERMEDIATE_4_WXY[2].price - w_length), 6
                ),
            },
            "final_Y_zigzag": {
                "A_length": round(final_a_length, 6),
                "B_retracement_of_A": ratio(
                    FINAL_Y_ZIGZAG[2].price - FINAL_Y_ZIGZAG[1].price,
                    final_a_length,
                ),
                "C_length": round(final_c_length, 6),
                "C_to_A": ratio(final_c_length, final_a_length),
            },
            "current_reversal": {
                "five_wave_advance": [
                    REVERSAL_WAVE_1[0].price,
                    REVERSAL_WAVE_1[-1].price,
                ],
                "current_close": current_price,
                "pullback_retracement_of_advance": ratio(
                    REVERSAL_WAVE_1[-1].price - current_price,
                    reversal_advance,
                ),
                "reversal_count_invalidation": REVERSAL_WAVE_1[0].price,
            },
        },
        "rsi_volume": {
            "Intermediate_3": motive_by_timeframe(INTERMEDIATE_3, snapshots),
            "final_Y_wave_A": motive_by_timeframe(FINAL_Y_WAVE_A, snapshots),
            "final_Y_wave_C": motive_by_timeframe(FINAL_Y_WAVE_C, snapshots),
            "reversal_wave_1_candidate": motive_by_timeframe(
                REVERSAL_WAVE_1, snapshots
            ),
            "March_vs_July_low": divergence,
        },
        "decision_levels": {
            "working_low": 3.57,
            "hard_standard_impulse_invalidation": 3.20,
            "first_reversal_evidence": 4.06,
            "stronger_reversal_evidence": 4.295,
            "structural_reversal_confirmation": 4.785,
            "major_confirmation": 6.64,
        },
        "verdict": (
            "Intermediate (4) has a price-complete W-X-Y candidate at 3.57, "
            "supported by near W/Y equality and a completed terminal 5-3-5 "
            "zigzag. A lower-degree five-wave advance reached 4.06, and its deep "
            "corrective pullback remains above 3.57. The low is not confirmed "
            "until that pullback holds and price breaks above 4.06."
        ),
    }


def main() -> None:
    audit = build_audit()
    output = ROOT / "tmc_fresh_count_20260726.json"
    output.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"analysis_id": audit["analysis_id"], "output": str(output)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
