from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from audit_tsla_count_20260721 import (
    add_indicators,
    impulse_rules,
    load_snapshot,
    segment_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DATE = "20260727"
TIMEFRAMES = ("monthly", "weekly", "daily", "4h", "1h", "15m", "5m", "1m")


def point(timestamp: str, price: float) -> dict[str, Any]:
    return {"timestamp": timestamp, "price": price}


def ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hard_rule_passed(result: dict[str, Any]) -> bool:
    return all(
        result[key]
        for key in (
            "wave_3_not_shortest_arithmetic",
            "wave_3_not_shortest_log",
            "wave_4_no_overlap",
        )
    )


def build_audit() -> dict[str, Any]:
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    sources: dict[str, Any] = {}
    for timeframe in TIMEFRAMES:
        path = ROOT / f"tv_tsla_{timeframe}_{SNAPSHOT_DATE}.json"
        metadata, rows = load_snapshot(path)
        snapshots[timeframe] = (metadata, add_indicators(rows))
        sources[timeframe] = {
            "path": str(path),
            "sha256": sha256(path),
            "bars": len(rows),
            "start": rows[0]["timestamp"].isoformat(),
            "end": rows[-1]["timestamp"].isoformat(),
            "feed": metadata.get("resolved_symbol"),
            "exchange": metadata.get("exchange"),
            "resolution": metadata.get("resolution"),
            "session": metadata.get("session"),
        }

    listed_history = {
        "family": "completed five-wave motive candidate",
        "degree_status": "exact absolute degree unresolved",
        "path": [
            point("2010-07-07T13:30:00+00:00", 0.998666),
            point("2014-09-04T13:30:00+00:00", 19.427981),
            point("2016-02-09T14:30:00+00:00", 9.403324),
            point("2021-11-04T13:30:00+00:00", 414.496252),
            point("2023-01-06T14:30:00+00:00", 101.81),
            point("2025-12-22T14:30:00+00:00", 498.83),
        ],
        "waves": {
            "I": {
                "family": "impulse candidate",
                "path": [
                    point("2010-07-07T13:30:00+00:00", 0.998666),
                    point("2010-12-01T14:30:00+00:00", 2.427998),
                    point("2011-02-23T14:30:00+00:00", 1.407332),
                    point("2013-09-30T13:30:00+00:00", 12.966654),
                    point("2013-11-26T14:30:00+00:00", 7.739992),
                    point("2014-09-04T13:30:00+00:00", 19.427981),
                ],
            },
            "II": {
                "family": "flat candidate",
                "path": [
                    point("2014-09-04T13:30:00+00:00", 19.427981),
                    point("2015-03-27T13:30:00+00:00", 12.093321),
                    point("2015-07-20T13:30:00+00:00", 19.109981),
                    point("2016-02-09T14:30:00+00:00", 9.403324),
                ],
            },
            "III": {
                "family": "extended impulse candidate",
                "path": [
                    point("2016-02-09T14:30:00+00:00", 9.403324),
                    point("2017-09-18T13:30:00+00:00", 25.973974),
                    point("2019-06-03T13:30:00+00:00", 11.799448),
                    point("2021-01-25T14:30:00+00:00", 300.133033),
                    point("2021-03-05T14:30:00+00:00", 179.82982),
                    point("2021-11-04T13:30:00+00:00", 414.496252),
                ],
            },
            "IV": {
                "family": "zigzag candidate; A requires an allowed leading diagonal",
                "path": [
                    point("2021-11-04T13:30:00+00:00", 414.496252),
                    point("2022-05-24T13:30:00+00:00", 206.85646),
                    point("2022-08-16T13:30:00+00:00", 314.666352),
                    point("2023-01-06T14:30:00+00:00", 101.81),
                ],
                "C_impulse": [
                    314.6664,
                    265.74,
                    313.8,
                    166.185,
                    198.92,
                    101.81,
                ],
            },
            "V": {
                "family": "ending-diagonal candidate; full 3-3-3-3-3 proof incomplete",
                "path": [
                    point("2023-01-06T14:30:00+00:00", 101.81),
                    point("2023-07-19T13:30:00+00:00", 299.29),
                    point("2024-04-22T13:30:00+00:00", 138.8025),
                    point("2024-12-18T14:30:00+00:00", 488.5399),
                    point("2025-04-07T13:30:00+00:00", 214.25),
                    point("2025-12-22T14:30:00+00:00", 498.83),
                ],
            },
        },
    }

    active_correction = {
        "family": "W-X-Y combination candidate",
        "status": "Y has a price-complete candidate at 306.51; reversal unconfirmed",
        "path": [
            point("2025-12-22T14:30:00+00:00", 498.83),
            point("2026-04-07T15:00:00+00:00", 337.25),
            point("2026-05-13T15:30:00+00:00", 453.4),
            point("2026-07-24T18:35:00+00:00", 306.51),
        ],
        "W": {
            "family": "nested double-three candidate",
            "path": [
                point("2025-12-22T14:30:00+00:00", 498.83),
                point("2026-02-05T15:15:00+00:00", 387.545),
                point("2026-02-11T14:30:00+00:00", 436.35),
                point("2026-04-07T15:00:00+00:00", 337.25),
            ],
            "first_w": {
                "family": "nested w-x-y candidate",
                "path": [
                    point("2025-12-22T14:30:00+00:00", 498.83),
                    point("2026-01-20T20:30:00+00:00", 417.46),
                    point("2026-01-23T16:00:00+00:00", 452.43),
                    point("2026-02-05T15:15:00+00:00", 387.545),
                ],
            },
            "terminal_y": {
                "family": "nested w-x-y candidate",
                "path": [
                    point("2026-02-11T14:30:00+00:00", 436.35),
                    point("2026-03-09T14:00:00+00:00", 381.4),
                    point("2026-03-11T14:00:00+00:00", 416.35),
                    point("2026-04-07T15:00:00+00:00", 337.25),
                ],
                "last_y": {
                    "family": "nested w-x-y candidate",
                    "path": [
                        point("2026-03-11T14:00:00+00:00", 416.35),
                        point("2026-03-20T19:30:00+00:00", 364.48),
                        point("2026-03-25T13:30:00+00:00", 396.23),
                        point("2026-04-07T15:00:00+00:00", 337.25),
                    ],
                    "deepest_y": [
                        point("2026-03-25T13:30:00+00:00", 396.23),
                        point("2026-03-30T19:30:00+00:00", 352.14),
                        point("2026-04-01T15:45:00+00:00", 383.13),
                        point("2026-04-07T15:00:00+00:00", 337.25),
                    ],
                },
            },
        },
        "X": {
            "family": "zigzag 5-3-5",
            "path": [
                point("2026-04-07T15:00:00+00:00", 337.25),
                point("2026-04-17T15:15:00+00:00", 409.29),
                point("2026-04-27T15:15:00+00:00", 364.02),
                point("2026-05-13T15:30:00+00:00", 453.4),
            ],
            "A_impulse": [337.25, 364.5, 337.26, 394.66, 381.81, 409.29],
            "C_impulse": [364.02, 396.48, 384.02, 449.16, 422.28, 453.4],
        },
        "Y": {
            "family": "w-x-y combination",
            "path": [
                point("2026-05-13T15:30:00+00:00", 453.4),
                point("2026-06-26T13:30:00+00:00", 368.6),
                point("2026-07-01T15:25:00+00:00", 432.85),
                point("2026-07-24T18:35:00+00:00", 306.51),
            ],
            "w": {
                "family": "nested w-x-y candidate",
                "path": [
                    point("2026-05-13T15:30:00+00:00", 453.4),
                    point("2026-05-19T14:45:00+00:00", 393.66),
                    point("2026-05-27T14:45:00+00:00", 445.59),
                    point("2026-06-26T13:30:00+00:00", 368.6),
                ],
            },
            "x": {
                "family": "zigzag candidate",
                "path": [
                    point("2026-06-26T13:30:00+00:00", 368.6),
                    point("2026-06-26T15:30:00+00:00", 387.8),
                    point("2026-06-26T19:30:00+00:00", 378.57),
                    point("2026-07-01T15:25:00+00:00", 432.85),
                ],
            },
            "y": {
                "family": "zigzag 5-3-5",
                "path": [
                    point("2026-07-01T15:25:00+00:00", 432.85),
                    point("2026-07-02T19:20:00+00:00", 389.31),
                    point("2026-07-06T19:45:00+00:00", 419.99),
                    point("2026-07-24T18:35:00+00:00", 306.51),
                ],
                "A_impulse": [
                    432.85,
                    423.73,
                    432.35,
                    394.25,
                    400.8,
                    389.31,
                ],
                "C_impulse": [
                    419.99,
                    390.52,
                    413.15,
                    369.435,
                    384.06,
                    306.51,
                ],
                "C_wave_3": [
                    413.15,
                    391.37,
                    406.6,
                    377.23,
                    386.52,
                    369.435,
                ],
                "C_wave_5": [
                    384.06,
                    374.89,
                    380.15,
                    315.735,
                    323.4,
                    306.51,
                ],
                "C_wave_5_wave_3": [
                    380.15,
                    332.8,
                    336.2,
                    318.56,
                    326.36,
                    315.735,
                ],
                "C_wave_5_wave_5": [
                    323.4,
                    317.51,
                    322.95,
                    308.5,
                    312.58,
                    306.51,
                ],
            },
        },
    }

    checks = {
        "listed_history_parent": impulse_rules(
            [0.998666, 19.427981, 9.403324, 414.496252, 101.81, 498.83]
        ),
        "listed_wave_I": impulse_rules(
            [0.998666, 2.427998, 1.407332, 12.966654, 7.739992, 19.427981]
        ),
        "listed_wave_III": impulse_rules(
            [9.403324, 25.973974, 11.799448, 300.133033, 179.82982, 414.496252]
        ),
        "listed_wave_IV_C": impulse_rules(
            [314.6664, 265.74, 313.8, 166.185, 198.92, 101.81]
        ),
        "active_X_A": impulse_rules(active_correction["X"]["A_impulse"]),
        "active_X_C": impulse_rules(active_correction["X"]["C_impulse"]),
        "active_Y_y_A": impulse_rules(active_correction["Y"]["y"]["A_impulse"]),
        "active_Y_y_C": impulse_rules(active_correction["Y"]["y"]["C_impulse"]),
        "active_Y_y_C_wave_3": impulse_rules(
            active_correction["Y"]["y"]["C_wave_3"]
        ),
        "active_Y_y_C_wave_5": impulse_rules(
            active_correction["Y"]["y"]["C_wave_5"]
        ),
        "active_Y_y_C_wave_5_wave_3": impulse_rules(
            active_correction["Y"]["y"]["C_wave_5_wave_3"]
        ),
        "active_Y_y_C_wave_5_wave_5": impulse_rules(
            active_correction["Y"]["y"]["C_wave_5_wave_5"]
        ),
    }
    hard_rule_failures = [
        name for name, result in checks.items() if not hard_rule_passed(result)
    ]

    fifteen_minute = snapshots["15m"][1]
    terminal_metrics = {
        "C_wave_1": segment_metrics(
            fifteen_minute,
            "2026-07-06T19:45:00+00:00",
            "2026-07-08T18:45:00+00:00",
            "down",
        ),
        "C_wave_3": segment_metrics(
            fifteen_minute,
            "2026-07-10T16:45:00+00:00",
            "2026-07-20T19:45:00+00:00",
            "down",
        ),
        "C_wave_5": segment_metrics(
            fifteen_minute,
            "2026-07-21T14:45:00+00:00",
            "2026-07-24T18:30:00+00:00",
            "down",
        ),
    }

    outer_w = 498.83 - 337.25
    outer_y = 453.4 - 306.51
    internal_w = 453.4 - 368.6
    internal_y = 432.85 - 306.51
    terminal_a = 432.85 - 389.31
    terminal_c = 419.99 - 306.51
    motive_v = 498.83 - 101.81

    return {
        "analysis_id": "tsla-deep-count-20260727-v1",
        "symbol": "BATS:TSLA",
        "listed_exchange": "NASDAQ",
        "data_cutoff": sources["1m"]["end"],
        "sources": sources,
        "method": {
            "price_first": True,
            "log_geometry": "preferred for 2010-2025 high-degree expansion",
            "arithmetic_geometry": "used for the 2025-2026 correction",
            "indicator_policy": "RSI, volume, EWO, and MACD are evidence only and cannot invalidate a price-valid count",
            "ambiguity_policy": "retain every structurally valid alternate until a hard rule or explicit price level invalidates it",
        },
        "listed_history": listed_history,
        "active_correction": active_correction,
        "hard_rule_checks": checks,
        "hard_rule_failures": hard_rule_failures,
        "fibonacci": {
            "outer_X_retracement_of_W": ratio(453.4 - 337.25, outer_w),
            "outer_Y_to_W_at_306_51": ratio(outer_y, outer_w),
            "outer_Y_W_equality_target": round(453.4 - outer_w, 4),
            "internal_Y_y_to_w_at_306_51": ratio(internal_y, internal_w),
            "internal_Y_y_1_618_w_target": round(432.85 - 1.618 * internal_w, 4),
            "terminal_C_to_A_at_306_51": ratio(terminal_c, terminal_a),
            "terminal_C_2_618_A_target": round(419.99 - 2.618 * terminal_a, 4),
            "wave_V_retracement_at_306_51": ratio(498.83 - 306.51, motive_v),
            "wave_V_50_pct_retracement": round(498.83 - 0.5 * motive_v, 4),
            "wave_V_61_8_pct_retracement": round(498.83 - 0.618 * motive_v, 4),
            "outer_Y_1_236_W_target": round(453.4 - 1.236 * outer_w, 4),
        },
        "terminal_indicator_evidence_15m": terminal_metrics,
        "current_state": {
            "working_low": 306.51,
            "status": "micro fifth ended; larger Y/C completion remains unconfirmed",
            "supporting_evidence": [
                "The terminal C and every selected nested motive sequence pass Wave 3 and Wave 4 hard rules.",
                "The terminal C is 2.606 times A, almost exactly the 2.618 projection near 306.00.",
                "The smallest fifth from 323.40 to 306.51 completed five children and rebounded above its 312.58 Wave 4.",
            ],
            "contradictory_evidence": [
                "The terminal fifth has the strongest negative 15-minute EWO and MACD, not exhaustion divergence.",
                "Average and relative volume expanded sharply during the terminal fifth because of the earnings gap.",
                "Price has not recovered 323.40, the first higher-degree reversal threshold.",
            ],
            "confirmation_levels": [313.21, 323.4, 342.11, 384.06, 419.99, 432.85, 453.4],
            "extension_levels": [306.51, 300.32, 295.64, 291.82, 253.6],
            "hard_invalidation": {
                "terminal_low_candidate": "A lower low extends the final fifth or requires a lower-degree relabel.",
                "active_outer_Y_origin": 453.4,
                "completed_high_degree_top": 498.83,
            },
        },
        "alternates": [
            {
                "name": "same swing structure shifted by one Elliott degree",
                "status": "structurally valid and co-equal",
                "distinguishing_evidence": "listed history cannot establish an absolute Supercycle boundary",
            },
            {
                "name": "306.51 is only an internal third or first wave of a longer extension",
                "status": "still valid",
                "distinguishing_evidence": "continued lows without recovery above 323.40, then 342.11",
            },
            {
                "name": "completed local W-X-Y becomes a larger W or A",
                "status": "must remain available after a rebound",
                "distinguishing_evidence": "a later corrective retracement followed by another decline would introduce X2-Z or a larger B-C sequence",
            },
        ],
    }


def main() -> None:
    output = ROOT / "tsla_deep_count_20260727.json"
    result = build_audit()
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "analysis_id": result["analysis_id"],
                "data_cutoff": result["data_cutoff"],
                "hard_rule_failures": result["hard_rule_failures"],
                "working_low": result["current_state"]["working_low"],
                "output": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
