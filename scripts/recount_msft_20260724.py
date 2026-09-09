from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from scripts.audit_tsla_count_20260721 import (
    ROOT,
    add_indicators,
    impulse_rules,
    load_snapshot,
    pivot_dict,
    ratio,
    segment_metrics,
    zigzag,
)


SNAPSHOT_NAMES = ("monthly", "weekly", "daily", "4h", "1h", "30m", "15m")


def load_all(
    snapshot_date: str = "20260724",
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for name in SNAPSHOT_NAMES:
        metadata, rows = load_snapshot(ROOT / f"tv_msft_{name}_{snapshot_date}.json")
        snapshots[name] = (metadata, add_indicators(rows))
    return snapshots


def build_exploration(snapshot_date: str = "20260724") -> dict[str, Any]:
    snapshots = load_all(snapshot_date)
    ranges = {
        name: {
            "bars": len(rows),
            "start": rows[0]["timestamp"].isoformat(),
            "end": rows[-1]["timestamp"].isoformat(),
            "last_close": rows[-1]["close"],
            "metadata": metadata,
        }
        for name, (metadata, rows) in snapshots.items()
    }
    ladders = {
        "monthly_full": {
            "timeframe": "monthly",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["monthly"][1],
                        ranges["monthly"]["start"],
                        ranges["monthly"]["end"],
                        threshold,
                        start_kind="low",
                    )
                ]
                for threshold in (0.20, 0.30, 0.40, 0.50)
            },
        },
        "weekly_full": {
            "timeframe": "weekly",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["weekly"][1],
                        ranges["weekly"]["start"],
                        ranges["weekly"]["end"],
                        threshold,
                        start_kind="low",
                    )
                ]
                for threshold in (0.15, 0.25, 0.35, 0.50)
            },
        },
        "daily_2009_present": {
            "timeframe": "daily",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["daily"][1],
                        "2009-03-01T00:00:00+00:00",
                        ranges["daily"]["end"],
                        threshold,
                        start_kind="low",
                    )
                ]
                for threshold in (0.08, 0.12, 0.18, 0.25, 0.35)
            },
        },
        "daily_cycle_ii_1999_2009": {
            "timeframe": "daily",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["daily"][1],
                        "1999-12-01T00:00:00+00:00",
                        "2009-03-31T23:59:59+00:00",
                        threshold,
                        start_kind="high",
                    )
                ]
                for threshold in (0.08, 0.12, 0.18, 0.25)
            },
        },
        "daily_primary_4_2021_2022": {
            "timeframe": "daily",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["daily"][1],
                        "2021-11-01T00:00:00+00:00",
                        "2022-11-30T23:59:59+00:00",
                        threshold,
                        start_kind="high",
                    )
                ]
                for threshold in (0.05, 0.08, 0.12, 0.18)
            },
        },
        "daily_2022_present": {
            "timeframe": "daily",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["daily"][1],
                        "2022-01-01T00:00:00+00:00",
                        ranges["daily"]["end"],
                        threshold,
                        start_kind="high",
                    )
                ]
                for threshold in (0.05, 0.08, 0.12, 0.18)
            },
        },
        "four_hour_2023_present": {
            "timeframe": "4h",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["4h"][1],
                        "2023-01-01T00:00:00+00:00",
                        ranges["4h"]["end"],
                        threshold,
                        start_kind="low",
                    )
                ]
                for threshold in (0.04, 0.06, 0.09, 0.12)
            },
        },
        "one_hour_2025_present": {
            "timeframe": "1h",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["1h"][1],
                        "2025-01-01T00:00:00+00:00",
                        ranges["1h"]["end"],
                        threshold,
                        start_kind="low",
                    )
                ]
                for threshold in (0.025, 0.04, 0.06, 0.09)
            },
        },
        "one_hour_wave_5_2025": {
            "timeframe": "1h",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["1h"][1],
                        "2025-04-07T00:00:00+00:00",
                        "2025-07-31T23:59:59+00:00",
                        threshold,
                        start_kind="low",
                    )
                ]
                for threshold in (0.01, 0.015, 0.02, 0.03)
            },
        },
        "one_hour_current_correction": {
            "timeframe": "1h",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["1h"][1],
                        "2025-07-31T00:00:00+00:00",
                        ranges["1h"]["end"],
                        threshold,
                        start_kind="high",
                    )
                ]
                for threshold in (0.015, 0.025, 0.04, 0.06, 0.09)
            },
        },
        "fifteen_minute_live_c_wave": {
            "timeframe": "15m",
            "thresholds": {
                str(threshold): [
                    pivot_dict(pivot)
                    for pivot in zigzag(
                        snapshots["15m"][1],
                        "2026-07-16T00:00:00+00:00",
                        ranges["15m"]["end"],
                        threshold,
                        start_kind="high",
                    )
                ]
                for threshold in (0.005, 0.008, 0.012, 0.02)
            },
        },
    }
    monthly = snapshots["monthly"][1]
    broad_metrics = {
        "1986_1999_advance": segment_metrics(
            monthly,
            "1986-03-01T00:00:00+00:00",
            "1999-12-31T23:59:59+00:00",
            "up",
        ),
        "2009_present_advance": segment_metrics(
            monthly,
            "2009-03-01T00:00:00+00:00",
            ranges["monthly"]["end"],
            "up",
        ),
    }
    hard_rule_checks = {
        "cycle_ii_zigzag_a_1999_2002": impulse_rules(
            [59.9688, 44.063, 57.5, 20.125, 38.075, 20.705]
        ),
        "cycle_ii_zigzag_c_2007_2009": impulse_rules(
            [37.5, 26.87, 32.1, 17.5, 21.25, 14.87]
        ),
        "cycle_i_1986_1999": impulse_rules(
            [0.0885, 0.5504, 0.2587, 6.8281, 5.0234, 59.9688]
        ),
        "cycle_i_1990_pivot_alternate": impulse_rules(
            [0.0885, 1.1215, 0.7049, 6.8281, 5.0234, 59.9688]
        ),
        "cycle_iii_2009_2025": impulse_rules(
            [14.87, 31.58, 22.73, 349.67, 213.431, 555.45]
        ),
        "cycle_iii_primary_3_2010_2021": impulse_rules(
            [22.73, 32.95, 26.26, 190.7, 132.52, 349.67]
        ),
        "cycle_iii_primary_3_intermediate_3_2012_2020": impulse_rules(
            [26.26, 50.045, 39.72, 116.18, 93.96, 190.7]
        ),
        "cycle_iii_primary_3_intermediate_5_2020_2021": impulse_rules(
            [132.52, 232.86, 196.25, 305.84, 280.25, 349.67]
        ),
        "cycle_iii_primary_5_2022_2025": impulse_rules(
            [213.431, 263.915, 219.35, 468.35, 344.79, 555.45]
        ),
        "cycle_iii_primary_4_first_leg_as_impulse_rejected": impulse_rules(
            [349.67, 276.05, 315.12, 270.0, 315.95, 241.51]
        ),
        "cycle_iii_primary_4_terminal_y_motive_candidate": impulse_rules(
            [294.18, 251.94, 267.45, 219.13, 251.04, 213.431]
        ),
        "primary_5_intermediate_3_2023_2024": impulse_rules(
            [219.35, 276.75, 245.63, 366.77, 309.45, 468.35]
        ),
        "primary_5_intermediate_5_2025": impulse_rules(
            [344.79, 394.66, 355.7, 437.0, 424.94, 555.45]
        ),
        "terminal_minor_5_2025": impulse_rules(
            [424.94, 460.25, 448.92, 500.79, 488.66, 555.45]
        ),
        "initial_decline_from_555_as_impulse": impulse_rules(
            [555.45, 492.37, 553.72, 464.89, 493.5, 356.28]
        ),
        "active_wave_a_2025_2026": impulse_rules(
            [555.45, 492.38, 553.5, 438.67, 483.52, 356.26]
        ),
        "active_wave_c_minor_1": impulse_rules(
            [466.33, 424.26, 436.1, 367.07, 378.88, 349.2]
        ),
        "rebound_349_to_405_as_standard_impulse_rejected": impulse_rules(
            [349.2, 380.5, 359.9, 395.57, 373.36, 405.975]
        ),
        "active_wave_c_intermediate_3_minor_1": impulse_rules(
            [405.975, 399.76, 401.83, 393.45, 398.39, 389.41]
        ),
        "active_wave_c_intermediate_3_minor_3_candidate": impulse_rules(
            [403.15, 396.33, 401.44, 386.96, 391.725, 377.39]
        ),
    }
    monthly = snapshots["monthly"][1]
    daily = snapshots["daily"][1]
    one_hour = snapshots["1h"][1]
    fifteen_minute = snapshots["15m"][1]
    indicator_metrics = {
        "cycle_iii_motive_waves_monthly": {
            "wave_1": segment_metrics(
                monthly,
                "2009-03-01T00:00:00+00:00",
                "2010-04-30T23:59:59+00:00",
                "up",
            ),
            "wave_3": segment_metrics(
                monthly,
                "2010-07-01T00:00:00+00:00",
                "2021-11-30T23:59:59+00:00",
                "up",
            ),
            "wave_5": segment_metrics(
                monthly,
                "2022-11-01T00:00:00+00:00",
                "2025-07-31T23:59:59+00:00",
                "up",
            ),
        },
        "primary_5_motive_waves_daily": {
            "wave_1": segment_metrics(
                daily,
                "2022-11-04T13:30:00+00:00",
                "2022-12-13T14:30:00+00:00",
                "up",
            ),
            "wave_3": segment_metrics(
                daily,
                "2023-01-06T14:30:00+00:00",
                "2024-07-05T13:30:00+00:00",
                "up",
            ),
            "wave_5": segment_metrics(
                daily,
                "2025-04-07T13:30:00+00:00",
                "2025-07-31T13:30:00+00:00",
                "up",
            ),
        },
        "intermediate_5_motive_waves_1h": {
            "wave_1": segment_metrics(
                one_hour,
                "2025-04-07T13:30:00+00:00",
                "2025-04-14T19:30:00+00:00",
                "up",
            ),
            "wave_3": segment_metrics(
                one_hour,
                "2025-04-21T13:30:00+00:00",
                "2025-05-01T14:30:00+00:00",
                "up",
            ),
            "wave_5": segment_metrics(
                one_hour,
                "2025-05-01T14:30:00+00:00",
                "2025-07-31T13:30:00+00:00",
                "up",
            ),
        },
        "terminal_minor_5_motive_waves_1h": {
            "wave_1": segment_metrics(
                one_hour,
                "2025-05-01T17:30:00+00:00",
                "2025-05-22T13:30:00+00:00",
                "up",
            ),
            "wave_3": segment_metrics(
                one_hour,
                "2025-05-23T13:30:00+00:00",
                "2025-06-30T13:30:00+00:00",
                "up",
            ),
            "wave_5": segment_metrics(
                one_hour,
                "2025-07-02T13:30:00+00:00",
                "2025-07-31T13:30:00+00:00",
                "up",
            ),
        },
        "active_primary_a_motive_waves_1h": {
            "wave_1": segment_metrics(
                one_hour,
                "2025-07-31T13:30:00+00:00",
                "2025-09-05T20:00:00+00:00",
                "down",
            ),
            "wave_3": segment_metrics(
                one_hour,
                "2025-10-28T13:30:00+00:00",
                "2026-01-21T20:00:00+00:00",
                "down",
            ),
            "wave_5": segment_metrics(
                one_hour,
                "2026-01-28T13:30:00+00:00",
                "2026-03-30T20:00:00+00:00",
                "down",
            ),
        },
        "active_primary_c_intermediate_1_motive_waves_1h": {
            "wave_1": segment_metrics(
                one_hour,
                "2026-06-01T13:30:00+00:00",
                "2026-06-03T20:00:00+00:00",
                "down",
            ),
            "wave_3": segment_metrics(
                one_hour,
                "2026-06-04T13:30:00+00:00",
                "2026-06-22T20:00:00+00:00",
                "down",
            ),
            "wave_5": segment_metrics(
                one_hour,
                "2026-06-24T13:30:00+00:00",
                "2026-06-25T20:00:00+00:00",
                "down",
            ),
        },
        "active_primary_c_intermediate_3_motive_waves_15m": {
            "wave_1": segment_metrics(
                fifteen_minute,
                "2026-07-16T19:00:00+00:00",
                "2026-07-17T15:15:00+00:00",
                "down",
            ),
            "wave_3_candidate": segment_metrics(
                fifteen_minute,
                "2026-07-20T16:30:00+00:00",
                "2026-07-23T15:00:00+00:00",
                "down",
            ),
        },
    }
    fibonacci_relationships = {
        "cycle_ii_1999_2009": {
            "retracement_of_cycle_i_arithmetic": ratio(
                59.9688 - 14.87,
                59.9688 - 0.0885,
            ),
            "b_retracement_of_a": ratio(
                37.5 - 20.705,
                59.9688 - 20.705,
            ),
            "c_to_a": ratio(
                37.5 - 14.87,
                59.9688 - 20.705,
            ),
            "c_0_618_target": round(
                37.5 - 0.618 * (59.9688 - 20.705),
                4,
            ),
        },
        "cycle_iii_primary_4_2021_2022": {
            "x_retracement_of_w": ratio(
                294.18 - 241.51,
                349.67 - 241.51,
            ),
            "y_to_w": ratio(
                294.18 - 213.431,
                349.67 - 241.51,
            ),
        },
        "cycle_iii_2009_2025": {
            "wave_2_retracement_of_wave_1": ratio(
                31.58 - 22.73,
                31.58 - 14.87,
            ),
            "wave_4_retracement_of_wave_3": ratio(
                349.67 - 213.431,
                349.67 - 22.73,
            ),
            "wave_3_to_wave_1": ratio(
                349.67 - 22.73,
                31.58 - 14.87,
            ),
            "wave_5_to_wave_3": ratio(
                555.45 - 213.431,
                349.67 - 22.73,
            ),
        },
        "cycle_iii_primary_5": {
            "wave_2_retracement_of_wave_1": ratio(
                263.915 - 219.35,
                263.915 - 213.431,
            ),
            "wave_4_retracement_of_wave_3": ratio(
                468.35 - 344.79,
                468.35 - 219.35,
            ),
            "wave_3_to_wave_1": ratio(
                468.35 - 219.35,
                263.915 - 213.431,
            ),
            "wave_5_to_wave_1": ratio(
                555.45 - 344.79,
                263.915 - 213.431,
            ),
            "wave_5_to_wave_3": ratio(
                555.45 - 344.79,
                468.35 - 219.35,
            ),
        },
        "terminal_intermediate_5": {
            "wave_3_to_wave_1": ratio(
                437.0 - 355.7,
                394.66 - 344.79,
            ),
            "wave_5_to_wave_1": ratio(
                555.45 - 424.94,
                394.66 - 344.79,
            ),
            "wave_5_to_wave_3": ratio(
                555.45 - 424.94,
                437.0 - 355.7,
            ),
        },
        "active_abc_zigzag": {
            "a_length": round(555.45 - 356.26, 4),
            "b_retracement_of_a": ratio(
                466.32 - 356.28,
                555.45 - 356.28,
            ),
            "c_0_618_target": round(
                466.32 - 0.618 * (555.45 - 356.28),
                4,
            ),
            "c_equality_target": round(
                466.32 - (555.45 - 356.28),
                4,
            ),
            "completed_c_candidate_to_a": ratio(
                466.33 - 349.2,
                555.45 - 356.26,
            ),
        },
        "active_c_internal": {
            "minor_1_length": round(466.32 - 349.2, 4),
            "minor_2_retracement_of_minor_1": ratio(
                405.99 - 349.2,
                466.32 - 349.2,
            ),
            "minor_3_1_0_projection": round(
                405.99 - (466.32 - 349.2),
                4,
            ),
            "minor_3_1_618_projection": round(
                405.99 - 1.618 * (466.32 - 349.2),
                4,
            ),
        },
        "active_c_intermediate_3": {
            "minor_2_retracement_of_minor_1": ratio(
                403.15 - 389.41,
                405.975 - 389.41,
            ),
            "minor_3_to_minor_1": ratio(
                403.15 - 377.39,
                405.975 - 389.41,
            ),
        },
    }
    return {
        "analysis_id": f"msft-fresh-blind-exploration-{snapshot_date}-v1",
        "ranges": ranges,
        "pivot_ladders": ladders,
        "broad_metrics": broad_metrics,
        "hard_rule_checks": hard_rule_checks,
        "indicator_metrics": indicator_metrics,
        "fibonacci_relationships": fibonacci_relationships,
    }


def print_ladder(name: str, ladder: dict[str, Any]) -> None:
    for threshold, pivots in ladder["thresholds"].items():
        print(
            f"\n{name} timeframe={ladder['timeframe']} "
            f"threshold={threshold} pivots={len(pivots)}"
        )
        print(
            " -> ".join(
                f"{pivot['timestamp'][:10]} {pivot['price']:.4f}"
                for pivot in pivots
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "msft_fresh_exploration_20260724.json",
    )
    parser.add_argument("--snapshot-date", default="20260724")
    parser.add_argument("--print-ladders", action="store_true")
    args = parser.parse_args()
    result = build_exploration(args.snapshot_date)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.print_ladders:
        for name, ladder in result["pivot_ladders"].items():
            print_ladder(name, ladder)
    else:
        print(
            json.dumps(
                {
                    "analysis_id": result["analysis_id"],
                    "output": str(args.output),
                    "ranges": {
                        name: {
                            "bars": item["bars"],
                            "start": item["start"],
                            "end": item["end"],
                            "last_close": item["last_close"],
                        }
                        for name, item in result["ranges"].items()
                    },
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
