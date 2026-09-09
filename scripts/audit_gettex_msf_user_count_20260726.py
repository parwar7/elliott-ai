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
TIMEFRAMES = ("daily", "4h", "1h", "30m", "15m")

OUTER_BOUNDARIES = [
    Boundary("2025-07-31T13:00:00+00:00", 491.90),
    Boundary("2025-09-05T19:00:00+00:00", 420.20),
    Boundary("2025-10-28T13:00:00+00:00", 479.05),
    Boundary("2025-12-16T14:00:00+00:00", 400.05),
    Boundary("2026-01-07T16:00:00+00:00", 418.80),
    Boundary("2026-03-27T14:00:00+00:00", 309.40),
]

USER_ABC_BOUNDARIES = [
    OUTER_BOUNDARIES[0],
    OUTER_BOUNDARIES[3],
    OUTER_BOUNDARIES[4],
    OUTER_BOUNDARIES[5],
]

BLUE_A_ZIGZAG_BOUNDARIES = OUTER_BOUNDARIES[:4]

BLUE_A_LEG_A_BOUNDARIES = [
    OUTER_BOUNDARIES[0],
    Boundary("2025-08-01T16:00:00+00:00", 451.35),
    Boundary("2025-08-05T11:00:00+00:00", 467.25),
    Boundary("2025-08-26T19:00:00+00:00", 428.10),
    Boundary("2025-08-28T08:00:00+00:00", 438.20),
    OUTER_BOUNDARIES[1],
]

BLUE_A_LEG_C_BOUNDARIES = [
    OUTER_BOUNDARIES[2],
    Boundary("2025-11-07T15:00:00+00:00", 425.70),
    Boundary("2025-11-13T06:30:00+00:00", 442.10),
    Boundary("2025-11-25T14:00:00+00:00", 401.90),
    Boundary("2025-12-02T17:00:00+00:00", 425.20),
    OUTER_BOUNDARIES[3],
]

RED_B_BOUNDARIES = [
    OUTER_BOUNDARIES[3],
    Boundary("2025-12-18T16:00:00+00:00", 417.10),
    Boundary("2026-01-02T18:00:00+00:00", 401.15),
    OUTER_BOUNDARIES[4],
]

ORANGE_C_BOUNDARIES = [
    OUTER_BOUNDARIES[4],
    Boundary("2026-01-21T19:00:00+00:00", 374.70),
    Boundary("2026-01-28T20:00:00+00:00", 404.75),
    Boundary("2026-02-05T21:00:00+00:00", 331.65),
    Boundary("2026-03-06T14:00:00+00:00", 357.10),
    OUTER_BOUNDARIES[5],
]


def load_snapshot(
    timeframe: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(
        (ROOT / f"tv_gettex_msf_{timeframe}_{SNAPSHOT_DATE}.json").read_text(
            encoding="utf-8"
        )
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
    boundaries: list[Boundary],
    rows: list[dict[str, Any]],
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


def leg_by_timeframe(
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
        result[timeframe] = segment_metrics(rows, boundaries[0], boundaries[-1])
    return result


def ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6)


def build_audit() -> dict[str, Any]:
    snapshots = {timeframe: load_snapshot(timeframe) for timeframe in TIMEFRAMES}
    outer_points = [boundary.price for boundary in OUTER_BOUNDARIES]
    orange_points = [boundary.price for boundary in ORANGE_C_BOUNDARIES]
    blue_leg_a_points = [boundary.price for boundary in BLUE_A_LEG_A_BOUNDARIES]
    blue_leg_c_points = [boundary.price for boundary in BLUE_A_LEG_C_BOUNDARIES]

    proposed_a_length = USER_ABC_BOUNDARIES[0].price - USER_ABC_BOUNDARIES[1].price
    proposed_b_retracement = (
        USER_ABC_BOUNDARIES[2].price - USER_ABC_BOUNDARIES[1].price
    )
    proposed_c_length = USER_ABC_BOUNDARIES[2].price - USER_ABC_BOUNDARIES[3].price

    orange_wave_1 = orange_points[0] - orange_points[1]
    orange_wave_3 = orange_points[2] - orange_points[3]
    orange_wave_3_target = orange_points[2] - 1.618 * orange_wave_1

    source = {
        timeframe: {
            key: snapshots[timeframe][0].get(key)
            for key in (
                "provider_symbol",
                "resolved_symbol",
                "exchange",
                "resolution",
                "session",
                "exchange_timezone",
                "dividends_adjusted",
                "captured_at",
            )
        }
        for timeframe in TIMEFRAMES
    }

    audit = {
        "analysis_id": "gettex-msf-user-335-count-audit-20260726-v1",
        "method": {
            "price": "arithmetic EUR; move is under one year",
            "rsi": "canonical Wilder RSI(14) on each same-feed timeframe",
            "volume": "raw GETTEX volume plus average volume relative to trailing 50 bars",
            "excluded_indicators": ["EWO", "MACD"],
        },
        "source": source,
        "pivots": {
            "outer_or_impulse": [
                {"timestamp": item.timestamp, "price": item.price}
                for item in OUTER_BOUNDARIES
            ],
            "user_proposed_abc": [
                {"timestamp": item.timestamp, "price": item.price}
                for item in USER_ABC_BOUNDARIES
            ],
            "orange_c": [
                {"timestamp": item.timestamp, "price": item.price}
                for item in ORANGE_C_BOUNDARIES
            ],
        },
        "price_validation": {
            "outer_impulse_alternate": impulse_rules(outer_points),
            "blue_a_leg_a_impulse": impulse_rules(blue_leg_a_points),
            "blue_a_leg_c_impulse": impulse_rules(blue_leg_c_points),
            "orange_c_impulse": impulse_rules(orange_points),
            "user_abc": {
                "a_length": round(proposed_a_length, 6),
                "b_retracement_length": round(proposed_b_retracement, 6),
                "b_retracement_of_a": ratio(
                    proposed_b_retracement, proposed_a_length
                ),
                "c_length": round(proposed_c_length, 6),
                "c_to_a_ratio": ratio(proposed_c_length, proposed_a_length),
                "canonical_flat_90_percent_b_test": (
                    proposed_b_retracement / proposed_a_length >= 0.90
                ),
            },
            "outer_fibonacci": {
                "wave_2_retracement_of_wave_1": ratio(
                    outer_points[2] - outer_points[1],
                    outer_points[0] - outer_points[1],
                ),
                "wave_3_to_wave_1": ratio(
                    outer_points[2] - outer_points[3],
                    outer_points[0] - outer_points[1],
                ),
                "wave_4_retracement_of_wave_3": ratio(
                    outer_points[4] - outer_points[3],
                    outer_points[2] - outer_points[3],
                ),
                "wave_5_to_wave_1": ratio(
                    outer_points[4] - outer_points[5],
                    outer_points[0] - outer_points[1],
                ),
            },
            "orange_fibonacci": {
                "wave_2_retracement_of_wave_1": ratio(
                    orange_points[2] - orange_points[1], orange_wave_1
                ),
                "wave_3_to_wave_1": ratio(orange_wave_3, orange_wave_1),
                "wave_3_1_618_target": round(orange_wave_3_target, 6),
                "wave_3_target_error_eur": round(
                    orange_points[3] - orange_wave_3_target, 6
                ),
                "wave_3_target_error_pct": round(
                    abs(orange_points[3] / orange_wave_3_target - 1.0) * 100.0,
                    6,
                ),
                "wave_4_retracement_of_wave_3": ratio(
                    orange_points[4] - orange_points[3], orange_wave_3
                ),
                "wave_5_to_wave_1": ratio(
                    orange_points[4] - orange_points[5], orange_wave_1
                ),
            },
        },
        "rsi_volume": {
            "outer_impulse_alternate": motive_by_timeframe(
                OUTER_BOUNDARIES, snapshots
            ),
            "orange_c_impulse": motive_by_timeframe(
                ORANGE_C_BOUNDARIES, snapshots
            ),
            "user_a_leg": leg_by_timeframe(
                [USER_ABC_BOUNDARIES[0], USER_ABC_BOUNDARIES[1]], snapshots
            ),
            "user_b_leg": leg_by_timeframe(
                [USER_ABC_BOUNDARIES[1], USER_ABC_BOUNDARIES[2]], snapshots
            ),
            "user_c_leg": leg_by_timeframe(
                [USER_ABC_BOUNDARIES[2], USER_ABC_BOUNDARIES[3]], snapshots
            ),
        },
        "structural_findings": {
            "blue_a": (
                "Price-valid 5-3-5 zigzag candidate. Its A and C legs each pass "
                "motive hard rules on the selected pivots."
            ),
            "red_b": (
                "Corrective three-wave rebound. Lower-timeframe price action is "
                "overlapping; the first and final rises can be resolved as motive "
                "legs, while the middle decline is corrective."
            ),
            "orange_c": (
                "Price-valid five-wave decline. Wave 3 is near the 1.618 extension "
                "of Wave 1 and Wave 4 does not overlap Wave 1."
            ),
            "degree_ambiguity": (
                "The same outer pivots also form a complete price-valid 1-2-3-4-5 "
                "impulse: blue A/B/C become Waves 1/2/3, red A-B-C becomes Wave 4, "
                "and the orange five becomes an extended Wave 5."
            ),
            "flat_family_issue": (
                "The proposed larger B retraces only about 20.4% of A. That fails "
                "the canonical 90% Flat-B threshold, so the 3-3-5 shape should not "
                "be called a regular, expanded, or running flat."
            ),
        },
    }
    return audit


def main() -> None:
    audit = build_audit()
    output = ROOT / "gettex_msf_user_count_audit_20260726.json"
    output.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "analysis_id": audit["analysis_id"],
                "output": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
