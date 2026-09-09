from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
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
TIMEFRAMES = ("4h", "1h", "15m", "5m")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def downward_diagonal_geometry(
    timestamps: list[str], prices: list[float]
) -> dict[str, Any]:
    if len(timestamps) != 6 or len(prices) != 6:
        raise ValueError("A diagonal check requires six boundary points.")

    times = [parse_timestamp(value) for value in timestamps]

    def log_slope(left: int, right: int) -> float:
        days = (times[right] - times[left]).total_seconds() / 86400.0
        return math.log(prices[right] / prices[left]) / days

    lower_slope = log_slope(1, 3)
    upper_slope = log_slope(2, 4)
    elapsed_to_five = (times[5] - times[1]).total_seconds() / 86400.0
    projected_lower_at_five = math.exp(
        math.log(prices[1]) + lower_slope * elapsed_to_five
    )
    actionary_lengths = [
        prices[0] - prices[1],
        prices[2] - prices[3],
        prices[4] - prices[5],
    ]

    return {
        "direction": "down",
        "wave_4_overlaps_wave_1": prices[4] > prices[1],
        "wave_3_not_shortest": actionary_lengths[1]
        > min(actionary_lengths[0], actionary_lengths[2]),
        "lower_log_slope_per_day": round(lower_slope, 9),
        "upper_log_slope_per_day": round(upper_slope, 9),
        "boundaries_converge": upper_slope < lower_slope,
        "actionary_lengths": [round(value, 6) for value in actionary_lengths],
        "strictly_contracting_actionary": (
            actionary_lengths[0] > actionary_lengths[1] > actionary_lengths[2]
        ),
        "projected_lower_at_wave_5_time": round(projected_lower_at_five, 4),
        "wave_5_throw_under_pct": round(
            100.0 * (prices[5] / projected_lower_at_five - 1.0),
            4,
        ),
    }


def build_audit() -> dict[str, Any]:
    snapshots: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, Any] = {}
    for timeframe in TIMEFRAMES:
        path = ROOT / f"tv_tsla_{timeframe}_{SNAPSHOT_DATE}.json"
        metadata, rows = load_snapshot(path)
        snapshots[timeframe] = add_indicators(rows)
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

    proposed_a_times = [
        "2025-12-22T14:30:00+00:00",
        "2026-02-05T15:15:00+00:00",
        "2026-02-11T14:30:00+00:00",
        "2026-03-20T19:30:00+00:00",
        "2026-03-25T13:30:00+00:00",
        "2026-04-07T15:00:00+00:00",
    ]
    proposed_a_prices = [498.83, 387.545, 436.35, 364.48, 396.23, 337.25]
    proposed_c1_times = [
        "2026-05-13T15:30:00+00:00",
        "2026-05-19T14:45:00+00:00",
        "2026-05-27T14:45:00+00:00",
        "2026-06-10T19:15:00+00:00",
        "2026-06-15T13:30:00+00:00",
        "2026-06-26T13:30:00+00:00",
    ]
    proposed_c1_prices = [453.4, 393.66, 445.59, 380.15, 416.0, 368.6]

    motive_children = {
        "A_diagonal_wave_1": [498.83, 476.81, 489.08, 417.46, 452.43, 387.545],
        "A_diagonal_wave_3": [436.35, 420.03, 436.22, 381.4, 416.35, 364.48],
        "A_diagonal_wave_5": [396.23, 389.3, 394.81, 352.14, 383.13, 337.25],
        "C_wave_1_diagonal_wave_1": [
            453.4,
            441.17,
            451.98,
            411.03,
            418.86,
            393.66,
        ],
        "C_wave_1_diagonal_wave_3": [
            445.59,
            428.16,
            441.06,
            388.6,
            418.5,
            380.15,
        ],
        "C_wave_1_diagonal_wave_5": [
            416.0,
            384.7,
            414.74,
            371.24,
            378.21,
            368.6,
        ],
        "C_wave_2_A": [368.6, 373.97, 371.02, 379.12, 376.38, 387.8],
        "C_wave_2_C": [378.57, 394.34, 389.18, 413.27, 406.13, 432.85],
        "C_wave_3_i": [432.85, 423.73, 432.35, 394.25, 400.8, 389.31],
        "C_wave_3_iii": [419.99, 390.52, 413.15, 369.435, 384.06, 306.51],
    }
    hard_rule_checks = {
        name: impulse_rules(points) for name, points in motive_children.items()
    }
    hard_rule_failures = [
        name
        for name, result in hard_rule_checks.items()
        if not hard_rule_passed(result)
    ]

    segments = {
        "C_wave_1_leading_diagonal": (
            "2026-05-13T15:30:00+00:00",
            "2026-06-26T13:30:00+00:00",
            "down",
        ),
        "C_wave_2_zigzag": (
            "2026-06-26T13:30:00+00:00",
            "2026-07-01T15:25:00+00:00",
            "up",
        ),
        "C_wave_3_active": (
            "2026-07-01T15:25:00+00:00",
            "2026-07-24T18:35:00+00:00",
            "down",
        ),
        "C_wave_3_i": (
            "2026-07-01T15:25:00+00:00",
            "2026-07-02T19:20:00+00:00",
            "down",
        ),
        "C_wave_3_ii": (
            "2026-07-02T19:20:00+00:00",
            "2026-07-06T19:45:00+00:00",
            "up",
        ),
        "C_wave_3_iii_candidate": (
            "2026-07-06T19:45:00+00:00",
            "2026-07-24T18:35:00+00:00",
            "down",
        ),
    }
    indicator_evidence = {
        timeframe: {
            name: segment_metrics(snapshots[timeframe], *segment)
            for name, segment in segments.items()
        }
        for timeframe in TIMEFRAMES
    }

    proposed_a = 498.83 - 337.25
    proposed_b_retracement = (453.4 - 337.25) / proposed_a
    c_wave_1 = 453.4 - 368.6
    c_wave_3_at_low = 432.85 - 306.51
    c3_i = 432.85 - 389.31
    c3_iii = 419.99 - 306.51
    c3_iii_range = 419.99 - 306.51

    return {
        "analysis_id": "tsla-user-abc-validation-20260727-v1",
        "symbol": "BATS:TSLA",
        "data_cutoff": sources["5m"]["end"],
        "sources": sources,
        "proposed_count": {
            "family": "A-B-C zigzag candidate",
            "A": {
                "path": proposed_a_prices,
                "family": "contracting leading diagonal, 5-3-5-3-5",
                "geometry": downward_diagonal_geometry(
                    proposed_a_times, proposed_a_prices
                ),
            },
            "B": {
                "path": [337.25, 409.29, 364.02, 453.4],
                "family": "zigzag, 5-3-5",
                "retracement_of_A": round(proposed_b_retracement, 6),
            },
            "C": {
                "status": "active",
                "wave_1": {
                    "path": proposed_c1_prices,
                    "family": "contracting leading diagonal, 5-3-5-3-5",
                    "geometry": downward_diagonal_geometry(
                        proposed_c1_times, proposed_c1_prices
                    ),
                },
                "wave_2": {
                    "path": [368.6, 387.8, 378.57, 432.85],
                    "family": "zigzag, 5-3-5",
                    "retracement_of_wave_1": round(
                        (432.85 - 368.6) / c_wave_1, 6
                    ),
                },
                "wave_3": {
                    "status": "active; internal iii may have ended at 306.51",
                    "path_so_far": [432.85, 389.31, 419.99, 306.51],
                    "internal_i": [432.85, 389.31],
                    "internal_ii": [389.31, 419.99],
                    "internal_iii_candidate": [419.99, 306.51],
                },
            },
        },
        "hard_rule_checks": hard_rule_checks,
        "hard_rule_failures": hard_rule_failures,
        "correction_semantics": {
            "valid_route": (
                "A is a five-wave leading diagonal, B is a three-wave zigzag, "
                "and C is an unfolding motive wave; this permits an A-B-C zigzag."
            ),
            "invalid_route_if_green_leg_is_only_WXY": (
                "A three-wave W-X-Y cannot serve as motive A of a zigzag. "
                "The 71.88% B retracement is also below the 90% flat threshold, "
                "so that reading cannot be repaired by calling the structure a flat."
            ),
            "prior_WXY_count": (
                "The prior W-X-Y interpretation remains structurally possible "
                "because the same nested swings admit corrective grouping."
            ),
        },
        "indicator_evidence": indicator_evidence,
        "fibonacci": {
            "B_retracement_of_A": round(proposed_b_retracement, 6),
            "C_wave_2_retracement_of_wave_1": round(
                (432.85 - 368.6) / c_wave_1, 6
            ),
            "C_wave_3_to_wave_1_at_306_51": round(
                c_wave_3_at_low / c_wave_1, 6
            ),
            "C_wave_3_1_618_wave_1_target": round(
                432.85 - 1.618 * c_wave_1, 4
            ),
            "C_wave_3_internal_iii_to_i_at_306_51": round(
                c3_iii / c3_i, 6
            ),
            "C_wave_3_internal_iii_2_618_i_target": round(
                419.99 - 2.618 * c3_i, 4
            ),
            "parent_C_equals_A_target": round(453.4 - proposed_a, 4),
            "parent_C_1_236_A_target": round(453.4 - 1.236 * proposed_a, 4),
            "possible_internal_iv_retracements": {
                "23.6_pct": round(306.51 + 0.236 * c3_iii_range, 4),
                "38.2_pct": round(306.51 + 0.382 * c3_iii_range, 4),
                "50_pct": round(306.51 + 0.5 * c3_iii_range, 4),
                "61.8_pct": round(306.51 + 0.618 * c3_iii_range, 4),
                "hard_wave_1_overlap_boundary": 389.31,
            },
        },
        "verdict": {
            "preferred_at_cutoff": (
                "A-B-C zigzag with leading-diagonal A and active C Wave 3"
            ),
            "why_preferred": [
                "Both proposed leading diagonals have converging boundaries, required overlap, and legal motive children.",
                "The count uses a simpler motive hierarchy than the deeply recursive W-X-Y interpretation.",
                "C Wave 3 has materially stronger negative EWO and MACD and lower RSI than C Wave 1 on every audited timeframe.",
                "The 306.51 low is a near-exact 2.618 extension of internal Wave i, which is characteristic of an extended internal iii.",
                "The latest decline shows momentum and volume expansion rather than terminal exhaustion divergence.",
            ],
            "unresolved": [
                "A leading diagonal and a nested W-X-Y can share the same visible pivots, so price structure does not eliminate the W-X-Y alternate.",
                "Only the smallest fifth at 306.51 is confirmed ended; internal iii and larger Wave 3 are not yet confirmed complete.",
            ],
            "distinguishing_evidence": {
                "below_306_51": "Internal iii is extending.",
                "above_323_40": "Supports an internal Wave iv rebound but does not complete larger Wave 3.",
                "wave_iv_zone": [333.29, 349.86, 363.25, 376.64],
                "above_389_31_before_new_low": (
                    "Violates normal impulse Wave iv overlap and weakens the active-third count."
                ),
                "later_new_low_after_wave_iv": (
                    "Supports the expected internal Wave v and the user's active C Wave 3 interpretation."
                ),
                "sustained_recovery_above_432_85": (
                    "Invalidates the interpretation that C Wave 3 remains active from that high."
                ),
            },
        },
        "policy": {
            "price_first": True,
            "indicator_role": (
                "Volume, RSI, EWO, and MACD are supporting or contradictory "
                "evidence only; they do not create hard Elliott invalidations."
            ),
            "volume_scope": (
                "Cboe One/BATS volume is compared only within the same feed and timeframe."
            ),
        },
    }


def main() -> None:
    output = ROOT / "tsla_user_abc_validation_20260727.json"
    result = build_audit()
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "analysis_id": result["analysis_id"],
                "data_cutoff": result["data_cutoff"],
                "hard_rule_failures": result["hard_rule_failures"],
                "preferred_at_cutoff": result["verdict"]["preferred_at_cutoff"],
                "output": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
