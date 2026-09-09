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

from elliott_ai.indicators import calculate_wilder_rsi
from scripts.audit_tsla_count_20260721 import impulse_rules


@dataclass(frozen=True)
class Boundary:
    timestamp: str
    price: float


SNAPSHOT_DATE = "20260725"
TIMEFRAMES = ("monthly", "weekly", "daily", "4h", "1h", "30m", "15m")


SEQUENCES: dict[str, dict[str, Any]] = {
    "outer_primary_a_impulse": {
        "timeframe": "1h",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2025-07-31T13:30:00+00:00", 555.45),
            Boundary("2025-09-05T18:30:00+00:00", 492.38),
            Boundary("2025-10-28T13:30:00+00:00", 553.50),
            Boundary("2026-01-21T18:30:00+00:00", 438.67),
            Boundary("2026-01-28T14:30:00+00:00", 483.52),
            Boundary("2026-03-30T18:30:00+00:00", 356.26),
        ],
        "motive": True,
    },
    "w_wave_a_impulse": {
        "timeframe": "30m",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2025-07-31T13:30:00+00:00", 555.45),
            Boundary("2025-08-01T17:00:00+00:00", 520.88),
            Boundary("2025-08-04T17:30:00+00:00", 538.25),
            Boundary("2025-08-26T18:30:00+00:00", 498.51),
            Boundary("2025-08-28T19:30:00+00:00", 510.90),
            Boundary("2025-09-05T19:00:00+00:00", 492.38),
        ],
        "motive": True,
    },
    "w_wave_b_correction": {
        "timeframe": "30m",
        "labels": ["a", "b", "c"],
        "boundaries": [
            Boundary("2025-09-05T19:00:00+00:00", 492.38),
            Boundary("2025-10-06T19:30:00+00:00", 531.01),
            Boundary("2025-10-14T13:30:00+00:00", 506.01),
            Boundary("2025-10-28T13:30:00+00:00", 553.50),
        ],
        "motive": False,
    },
    "w_wave_c_impulse": {
        "timeframe": "1h",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2025-10-28T13:30:00+00:00", 553.50),
            Boundary("2025-11-25T14:30:00+00:00", 464.89),
            Boundary("2025-12-02T17:30:00+00:00", 493.44),
            Boundary("2026-02-05T20:30:00+00:00", 392.33),
            Boundary("2026-02-10T14:30:00+00:00", 423.68),
            Boundary("2026-03-30T18:30:00+00:00", 356.26),
        ],
        "motive": True,
    },
    "primary_b_or_x_correction": {
        "timeframe": "1h",
        "labels": ["a", "b", "c"],
        "boundaries": [
            Boundary("2026-03-30T18:30:00+00:00", 356.26),
            Boundary("2026-04-22T19:30:00+00:00", 434.00),
            Boundary("2026-04-30T16:30:00+00:00", 398.01),
            Boundary("2026-06-01T13:30:00+00:00", 466.33),
        ],
        "motive": False,
    },
    "primary_b_or_x_wave_a_impulse": {
        "timeframe": "30m",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2026-03-30T18:30:00+00:00", 356.85),
            Boundary("2026-04-08T13:30:00+00:00", 385.00),
            Boundary("2026-04-09T14:30:00+00:00", 367.06),
            Boundary("2026-04-17T15:00:00+00:00", 431.58),
            Boundary("2026-04-20T15:00:00+00:00", 416.32),
            Boundary("2026-04-22T19:30:00+00:00", 434.00),
        ],
        "motive": True,
    },
    "primary_b_or_x_wave_b_correction": {
        "timeframe": "30m",
        "labels": ["a", "b", "c"],
        "boundaries": [
            Boundary("2026-04-22T19:30:00+00:00", 434.00),
            Boundary("2026-04-23T17:30:00+00:00", 411.41),
            Boundary("2026-04-28T19:30:00+00:00", 429.92),
            Boundary("2026-04-30T16:30:00+00:00", 398.01),
        ],
        "motive": False,
    },
    "primary_b_or_x_wave_c_diagonal_candidate": {
        "timeframe": "30m",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2026-04-30T16:30:00+00:00", 398.01),
            Boundary("2026-05-07T13:30:00+00:00", 427.98),
            Boundary("2026-05-14T13:30:00+00:00", 400.88),
            Boundary("2026-05-19T13:30:00+00:00", 432.70),
            Boundary("2026-05-27T13:30:00+00:00", 409.57),
            Boundary("2026-06-01T14:00:00+00:00", 466.33),
        ],
        "motive": False,
    },
    "primary_c_or_y_wave_a_impulse": {
        "timeframe": "1h",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2026-06-01T13:30:00+00:00", 466.33),
            Boundary("2026-06-03T16:30:00+00:00", 424.26),
            Boundary("2026-06-04T13:30:00+00:00", 436.10),
            Boundary("2026-06-22T19:30:00+00:00", 367.07),
            Boundary("2026-06-24T13:30:00+00:00", 378.88),
            Boundary("2026-06-25T17:30:00+00:00", 349.20),
        ],
        "motive": True,
    },
    "direct_primary_c_partial_impulse": {
        "timeframe": "1h",
        "labels": ["(1)", "(2)", "(3)", "(4)", "(5)-active"],
        "boundaries": [
            Boundary("2026-06-01T13:30:00+00:00", 466.33),
            Boundary("2026-06-03T16:30:00+00:00", 424.26),
            Boundary("2026-06-04T13:30:00+00:00", 436.10),
            Boundary("2026-06-25T17:30:00+00:00", 349.20),
            Boundary("2026-07-16T18:30:00+00:00", 405.975),
            Boundary("2026-07-24T19:30:00+00:00", 381.42),
        ],
        "motive": False,
        "partial_motive": True,
        "incomplete": True,
    },
    "direct_primary_c_wave_3_minor_impulse": {
        "timeframe": "30m",
        "labels": ["1", "2", "3", "4", "5"],
        "boundaries": [
            Boundary("2026-06-04T13:30:00+00:00", 436.10),
            Boundary("2026-06-12T13:30:00+00:00", 382.27),
            Boundary("2026-06-15T16:30:00+00:00", 401.75),
            Boundary("2026-06-22T19:30:00+00:00", 367.07),
            Boundary("2026-06-24T13:30:00+00:00", 378.88),
            Boundary("2026-06-25T17:30:00+00:00", 349.20),
        ],
        "motive": True,
    },
    "primary_c_or_y_wave_b_triple_three_candidate": {
        "timeframe": "1h",
        "labels": ["W", "X", "Y", "X2", "Z"],
        "boundaries": [
            Boundary("2026-06-25T17:30:00+00:00", 349.20),
            Boundary("2026-06-29T13:30:00+00:00", 380.50),
            Boundary("2026-06-29T19:30:00+00:00", 366.82),
            Boundary("2026-07-07T13:30:00+00:00", 395.57),
            Boundary("2026-07-09T13:30:00+00:00", 373.36),
            Boundary("2026-07-16T18:30:00+00:00", 405.975),
        ],
        "motive": False,
    },
    "primary_c_or_y_wave_b_z_subdivision": {
        "timeframe": "15m",
        "labels": ["a", "b", "c"],
        "boundaries": [
            Boundary("2026-07-09T13:30:00+00:00", 373.36),
            Boundary("2026-07-13T18:45:00+00:00", 393.64),
            Boundary("2026-07-14T13:30:00+00:00", 378.63),
            Boundary("2026-07-16T19:00:00+00:00", 405.975),
        ],
        "motive": False,
    },
    "active_c_or_y_wave_c_candidate": {
        "timeframe": "15m",
        "labels": ["i", "ii", "iii", "iv", "v-active"],
        "boundaries": [
            Boundary("2026-07-16T19:00:00+00:00", 405.975),
            Boundary("2026-07-17T15:15:00+00:00", 389.41),
            Boundary("2026-07-20T16:30:00+00:00", 403.15),
            Boundary("2026-07-23T15:00:00+00:00", 377.39),
            Boundary("2026-07-24T13:30:00+00:00", 389.00),
            Boundary("2026-07-24T19:45:00+00:00", 381.42),
        ],
        "motive": False,
        "incomplete": True,
    },
}


REPLAY_CHECKPOINTS = [
    {
        "cutoff": "2025-08-01",
        "pivot": 555.45,
        "observation": (
            "A major top was visible, but no completed down leg existed. "
            "Neither Primary A nor W could be assigned."
        ),
    },
    {
        "cutoff": "2025-09-05",
        "pivot": 492.38,
        "observation": (
            "The first decline completed a valid lower-timeframe five. "
            "It remained ambiguous between Wave 1 and Wave A."
        ),
    },
    {
        "cutoff": "2025-10-28",
        "pivot": 553.50,
        "observation": (
            "The rebound retraced almost all of the first decline and was corrective. "
            "It could still be outer Wave 2 or W-B."
        ),
    },
    {
        "cutoff": "2026-03-30",
        "pivot": 356.26,
        "observation": (
            "The first major decline was complete. It passed both an outer five-wave "
            "count and a nested 5-3-5 zigzag W count."
        ),
    },
    {
        "cutoff": "2026-06-01",
        "pivot": 466.33,
        "observation": (
            "A three-leg corrective rebound completed. It could be Primary B or X; "
            "RSI and volume could not determine the letter."
        ),
    },
    {
        "cutoff": "2026-06-25",
        "pivot": 349.20,
        "observation": (
            "A clean five down completed. The correction remained active unless this "
            "was the entire Primary C/Y termination."
        ),
    },
    {
        "cutoff": "2026-07-16",
        "pivot": 405.975,
        "observation": (
            "The rebound was overlapping and corrective, best represented as a "
            "complex B-wave candidate rather than a standard impulse."
        ),
    },
    {
        "cutoff": "2026-07-24",
        "pivot": 381.70,
        "observation": (
            "A lower-degree decline was active, but its fifth wave had not made a new "
            "low below 377.39, so the local five remained unconfirmed."
        ),
    },
]


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_snapshot(
    timeframe: str, snapshot_date: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(
        (ROOT / f"tv_msft_{timeframe}_{snapshot_date}.json").read_text(
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


def add_rsi_and_volume(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rsi = calculate_wilder_rsi([row["close"] for row in rows], period=14)["values"]
    for index, row in enumerate(rows):
        row["rsi"] = rsi[index]
        baseline_rows = rows[max(0, index - 49) : index + 1]
        baseline = sum(item["volume"] for item in baseline_rows) / len(baseline_rows)
        row["relative_volume_50"] = row["volume"] / baseline if baseline else None
    return rows


def selected_rows(
    rows: Iterable[dict[str, Any]], start: str, end: str
) -> list[dict[str, Any]]:
    start_at = parse_timestamp(start)
    end_at = parse_timestamp(end)
    return [row for row in rows if start_at <= row["timestamp"] <= end_at]


def finite_values(rows: Iterable[dict[str, Any]], field: str) -> list[float]:
    return [
        float(row[field])
        for row in rows
        if row.get(field) is not None and math.isfinite(float(row[field]))
    ]


def segment_metrics(
    rows: list[dict[str, Any]],
    start: Boundary,
    end: Boundary,
) -> dict[str, Any]:
    selected = selected_rows(rows, start.timestamp, end.timestamp)
    if not selected:
        return {"availability": "unavailable", "bars": 0}
    direction = "up" if end.price > start.price else "down"
    rsi = finite_values(selected, "rsi")
    relative_volume = finite_values(selected, "relative_volume_50")
    peak_volume_row = max(selected, key=lambda row: row["volume"])
    peak_relative_row = max(
        (row for row in selected if row.get("relative_volume_50") is not None),
        key=lambda row: row["relative_volume_50"],
        default=None,
    )
    return {
        "availability": "available",
        "direction": direction,
        "start": start.timestamp,
        "end": end.timestamp,
        "start_price": start.price,
        "end_price": end.price,
        "change_pct": round((end.price / start.price - 1.0) * 100.0, 6),
        "bars": len(selected),
        "cumulative_volume": round(sum(row["volume"] for row in selected), 2),
        "average_volume": round(
            sum(row["volume"] for row in selected) / len(selected), 2
        ),
        "average_relative_volume_50": (
            round(sum(relative_volume) / len(relative_volume), 6)
            if relative_volume
            else None
        ),
        "peak_relative_volume_50": (
            round(float(peak_relative_row["relative_volume_50"]), 6)
            if peak_relative_row is not None
            else None
        ),
        "peak_relative_volume_time": (
            peak_relative_row["timestamp"].isoformat()
            if peak_relative_row is not None
            else None
        ),
        "peak_raw_volume": round(float(peak_volume_row["volume"]), 2),
        "peak_raw_volume_time": peak_volume_row["timestamp"].isoformat(),
        "rsi_at_start_bar_close": round(float(rsi[0]), 6) if rsi else None,
        "rsi_at_end_bar_close": round(float(rsi[-1]), 6) if rsi else None,
        "rsi_minimum": round(min(rsi), 6) if rsi else None,
        "rsi_maximum": round(max(rsi), 6) if rsi else None,
        "rsi_extreme_in_wave_direction": (
            round(min(rsi), 6)
            if rsi and direction == "down"
            else round(max(rsi), 6)
            if rsi
            else None
        ),
        "proportion_rsi_above_50": (
            round(sum(value > 50.0 for value in rsi) / len(rsi), 6)
            if rsi
            else None
        ),
        "proportion_rsi_below_50": (
            round(sum(value < 50.0 for value in rsi) / len(rsi), 6)
            if rsi
            else None
        ),
    }


def motive_evidence(waves: list[dict[str, Any]]) -> dict[str, Any]:
    wave_1, wave_2, wave_3, wave_4, wave_5 = waves
    direction = wave_1["direction"]
    third_rsi_stronger = (
        wave_3["rsi_extreme_in_wave_direction"]
        < wave_1["rsi_extreme_in_wave_direction"]
        if direction == "down"
        else wave_3["rsi_extreme_in_wave_direction"]
        > wave_1["rsi_extreme_in_wave_direction"]
    )
    fifth_rsi_divergence = (
        wave_5["end_price"] < wave_3["end_price"]
        and wave_5["rsi_at_end_bar_close"] > wave_3["rsi_at_end_bar_close"]
        if direction == "down"
        else wave_5["end_price"] > wave_3["end_price"]
        and wave_5["rsi_at_end_bar_close"] < wave_3["rsi_at_end_bar_close"]
    )
    return {
        "wave_3_rsi_stronger_than_wave_1": third_rsi_stronger,
        "wave_3_average_relative_volume_vs_wave_1": round(
            wave_3["average_relative_volume_50"]
            / wave_1["average_relative_volume_50"],
            6,
        ),
        "wave_2_volume_dry_up_vs_wave_1": (
            wave_2["average_relative_volume_50"]
            < wave_1["average_relative_volume_50"]
        ),
        "wave_4_volume_dry_up_vs_wave_3": (
            wave_4["average_relative_volume_50"]
            < wave_3["average_relative_volume_50"]
        ),
        "wave_5_aligned_endpoint_rsi_divergence_vs_wave_3": fifth_rsi_divergence,
        "wave_5_average_relative_volume_dry_up_vs_wave_3": (
            wave_5["average_relative_volume_50"]
            < wave_3["average_relative_volume_50"]
        ),
        "wave_5_relative_volume_ratio_to_wave_3": round(
            wave_5["average_relative_volume_50"]
            / wave_3["average_relative_volume_50"],
            6,
        ),
        "note": (
            "RSI and volume are confirmation evidence only. Failure of divergence "
            "does not invalidate a price-valid impulse."
        ),
    }


def partial_motive_evidence(waves: list[dict[str, Any]]) -> dict[str, Any]:
    wave_1, wave_2, wave_3, wave_4, wave_5 = waves
    direction = wave_1["direction"]
    wave_3_rsi_stronger = (
        wave_3["rsi_extreme_in_wave_direction"]
        < wave_1["rsi_extreme_in_wave_direction"]
        if direction == "down"
        else wave_3["rsi_extreme_in_wave_direction"]
        > wave_1["rsi_extreme_in_wave_direction"]
    )
    wave_5_new_extreme = (
        wave_5["end_price"] < wave_3["end_price"]
        if direction == "down"
        else wave_5["end_price"] > wave_3["end_price"]
    )
    return {
        "wave_3_rsi_stronger_than_wave_1": wave_3_rsi_stronger,
        "wave_3_average_relative_volume_vs_wave_1": round(
            wave_3["average_relative_volume_50"]
            / wave_1["average_relative_volume_50"],
            6,
        ),
        "wave_2_volume_dry_up_vs_wave_1": (
            wave_2["average_relative_volume_50"]
            < wave_1["average_relative_volume_50"]
        ),
        "wave_4_volume_dry_up_vs_wave_3": (
            wave_4["average_relative_volume_50"]
            < wave_3["average_relative_volume_50"]
        ),
        "wave_5_new_price_extreme": wave_5_new_extreme,
        "wave_5_rsi_divergence_status": (
            "not_yet_comparable" if not wave_5_new_extreme else "comparable"
        ),
        "wave_5_current_relative_volume_ratio_to_wave_3": round(
            wave_5["average_relative_volume_50"]
            / wave_3["average_relative_volume_50"],
            6,
        ),
    }


def motive_confirmation_by_timeframe(
    boundaries: list[Boundary],
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    first_at = parse_timestamp(boundaries[0].timestamp)
    last_at = parse_timestamp(boundaries[-1].timestamp)
    for timeframe in ("daily", "4h", "1h", "30m", "15m"):
        rows = snapshots[timeframe][1]
        if first_at < rows[0]["timestamp"] or last_at > rows[-1]["timestamp"]:
            result[timeframe] = {"availability": "unavailable"}
            continue
        waves = [
            segment_metrics(rows, start, end)
            for start, end in zip(boundaries[:-1], boundaries[1:])
        ]
        if any(wave["availability"] != "available" for wave in waves):
            result[timeframe] = {"availability": "unavailable"}
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
        }
    return result


def build_sequence(
    spec: dict[str, Any], snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]
) -> dict[str, Any]:
    timeframe = spec["timeframe"]
    rows = snapshots[timeframe][1]
    boundaries: list[Boundary] = spec["boundaries"]
    waves = [
        {
            "label": label,
            **segment_metrics(rows, start, end),
        }
        for label, start, end in zip(
            spec["labels"], boundaries[:-1], boundaries[1:]
        )
    ]
    result: dict[str, Any] = {
        "timeframe": timeframe,
        "source": {
            key: snapshots[timeframe][0].get(key)
            for key in (
                "provider_symbol",
                "exchange",
                "resolution",
                "session",
                "volume_scope",
            )
        },
        "boundaries": [
            {"timestamp": boundary.timestamp, "price": boundary.price}
            for boundary in boundaries
        ],
        "waves": waves,
        "incomplete": bool(spec.get("incomplete")),
    }
    if spec.get("motive"):
        result["hard_price_rules"] = impulse_rules(
            [boundary.price for boundary in boundaries]
        )
        result["rsi_volume_confirmation"] = motive_evidence(waves)
        result["confirmation_by_timeframe"] = motive_confirmation_by_timeframe(
            boundaries, snapshots
        )
    elif spec.get("partial_motive"):
        direction = -1.0 if boundaries[-1].price < boundaries[0].price else 1.0
        wave_1_length = direction * (
            boundaries[1].price - boundaries[0].price
        )
        wave_3_length = direction * (
            boundaries[3].price - boundaries[2].price
        )
        wave_4_no_overlap = (
            boundaries[4].price < boundaries[1].price
            if direction < 0
            else boundaries[4].price > boundaries[1].price
        )
        result["partial_hard_price_rules"] = {
            "wave_3_longer_than_wave_1": wave_3_length > wave_1_length,
            "wave_4_no_overlap": wave_4_no_overlap,
            "wave_1_length": round(wave_1_length, 6),
            "wave_3_length": round(wave_3_length, 6),
            "wave_4_overlap_clearance": round(
                direction * (boundaries[4].price - boundaries[1].price), 6
            ),
            "wave_5_incomplete": True,
        }
        result["rsi_volume_confirmation"] = partial_motive_evidence(waves)
    return result


def pivot_context(
    boundary: Boundary,
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, Any]:
    pivot_at = parse_timestamp(boundary.timestamp)
    result: dict[str, Any] = {
        "timestamp": boundary.timestamp,
        "price": boundary.price,
        "timeframes": {},
    }
    for timeframe in ("weekly", "daily", "4h", "1h", "30m", "15m"):
        rows = snapshots[timeframe][1]
        eligible = [row for row in rows if row["timestamp"] <= pivot_at]
        if not eligible:
            result["timeframes"][timeframe] = {"availability": "unavailable"}
            continue
        row = eligible[-1]
        result["timeframes"][timeframe] = {
            "bar_timestamp": row["timestamp"].isoformat(),
            "close": row["close"],
            "rsi_14": round(float(row["rsi"]), 6) if row["rsi"] is not None else None,
            "relative_volume_50": (
                round(float(row["relative_volume_50"]), 6)
                if row["relative_volume_50"] is not None
                else None
            ),
        }
    return result


def build_audit(snapshot_date: str) -> dict[str, Any]:
    snapshots = {
        timeframe: load_snapshot(timeframe, snapshot_date)
        for timeframe in TIMEFRAMES
    }
    sequences = {
        name: build_sequence(spec, snapshots) for name, spec in SEQUENCES.items()
    }
    major_boundaries = [
        Boundary("2025-07-31T13:30:00+00:00", 555.45),
        Boundary("2025-09-05T18:30:00+00:00", 492.38),
        Boundary("2025-10-28T13:30:00+00:00", 553.50),
        Boundary("2026-03-30T18:30:00+00:00", 356.26),
        Boundary("2026-06-01T13:30:00+00:00", 466.33),
        Boundary("2026-06-25T17:30:00+00:00", 349.20),
        Boundary("2026-07-16T18:30:00+00:00", 405.975),
        Boundary("2026-07-24T19:30:00+00:00", 381.70),
    ]
    return {
        "analysis_id": f"msft-cycle-iv-rsi-volume-replay-{snapshot_date}-v1",
        "symbol": "NASDAQ:MSFT",
        "method": {
            "price_role": "Elliott hard-rule and child-structure validation",
            "indicators_used": ["Wilder RSI(14)", "raw volume", "relative volume(50)"],
            "indicators_excluded": ["EWO", "MACD", "AVWAP", "Bollinger Bands"],
            "rsi_method": (
                "Canonical Wilder RSI(14): initial simple average of 14 gains/losses, "
                "then Wilder recursive smoothing."
            ),
            "causality": (
                "Each historical RSI value is causal and uses only closes available "
                "through that bar. Segment metrics stop at the stated pivot cutoff."
            ),
        },
        "snapshot_metadata": {
            timeframe: snapshots[timeframe][0] for timeframe in TIMEFRAMES
        },
        "replay_checkpoints": REPLAY_CHECKPOINTS,
        "sequences": sequences,
        "major_pivot_context": [
            pivot_context(boundary, snapshots) for boundary in major_boundaries
        ],
        "data_integrity": {
            "daily_vs_intraday_2026_06_29_low": {
                "daily_low": 359.90,
                "one_hour_low": 366.82,
                "status": "incomparable_exact_pivot",
                "handling": (
                    "The lower-timeframe correction count uses the 1-hour Cboe One "
                    "pivot and does not substitute the unmatched daily wick."
                ),
            },
            "intrabar_order_correction": {
                "rejected_path": [555.45, 526.83, 535.79],
                "reason": (
                    "Thirty-minute bars show 535.79 occurred before 526.83 on "
                    "2025-08-01, so that ordering cannot define Wave 1 then Wave 2."
                ),
                "replacement_path": [
                    555.45,
                    520.88,
                    538.25,
                    498.51,
                    510.90,
                    492.38,
                ],
            },
        },
    }


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def motive_table(result: dict[str, Any]) -> list[str]:
    lines = [
        "| Wave | Path | RSI end | RSI extreme | Avg rel. volume | Bars |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for wave in result["waves"]:
        lines.append(
            "| {label} | ${start:.2f} -> ${end:.2f} | {rsi_end} | {rsi_extreme} | "
            "{rel_volume} | {bars} |".format(
                label=wave["label"],
                start=wave["start_price"],
                end=wave["end_price"],
                rsi_end=fmt(wave["rsi_at_end_bar_close"]),
                rsi_extreme=fmt(wave["rsi_extreme_in_wave_direction"]),
                rel_volume=fmt(wave["average_relative_volume_50"], 3),
                bars=wave["bars"],
            )
        )
    return lines


def cross_timeframe_table(result: dict[str, Any]) -> list[str]:
    lines = [
        "| Timeframe | W3/W1 rel. volume | W5/W3 rel. volume | "
        "W3 RSI end | W5 RSI end | W5 RSI divergence |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for timeframe, item in result["confirmation_by_timeframe"].items():
        if item["availability"] != "available":
            continue
        lines.append(
            "| {timeframe} | {wave_3_volume} | {wave_5_volume} | {wave_3_rsi} | "
            "{wave_5_rsi} | {divergence} |".format(
                timeframe=timeframe,
                wave_3_volume=fmt(
                    item["wave_3_to_wave_1_relative_volume_ratio"], 3
                ),
                wave_5_volume=fmt(
                    item["wave_5_to_wave_3_relative_volume_ratio"], 3
                ),
                wave_3_rsi=fmt(item["wave_3_rsi_end"]),
                wave_5_rsi=fmt(item["wave_5_rsi_end"]),
                divergence=fmt(item["wave_5_endpoint_rsi_divergence"]),
            )
        )
    return lines


def render_report(audit: dict[str, Any]) -> str:
    sequences = audit["sequences"]
    outer = sequences["outer_primary_a_impulse"]
    w_a = sequences["w_wave_a_impulse"]
    w_b = sequences["w_wave_b_correction"]
    w_c = sequences["w_wave_c_impulse"]
    rebound = sequences["primary_b_or_x_correction"]
    rebound_a = sequences["primary_b_or_x_wave_a_impulse"]
    rebound_b = sequences["primary_b_or_x_wave_b_correction"]
    rebound_c = sequences["primary_b_or_x_wave_c_diagonal_candidate"]
    y_a = sequences["primary_c_or_y_wave_a_impulse"]
    direct_c = sequences["direct_primary_c_partial_impulse"]
    direct_c_wave_3 = sequences["direct_primary_c_wave_3_minor_impulse"]
    y_b = sequences["primary_c_or_y_wave_b_triple_three_candidate"]
    y_b_z = sequences["primary_c_or_y_wave_b_z_subdivision"]
    active = sequences["active_c_or_y_wave_c_candidate"]
    lines = [
        "# MSFT Cycle IV RSI and Volume Replay Audit",
        "",
        "**Symbol:** NASDAQ:MSFT",
        "",
        "**TradingView feed:** BATS:MSFT / Cboe One, regular session",
        "",
        "**Data cutoff:** 24 July 2026",
        "",
        "## Method",
        "",
        "Price structure supplies the Elliott hard rules. The only indicators used "
        "for confirmation are canonical Wilder RSI(14) and volume. EWO, MACD, "
        "AVWAP and volatility bands are excluded from this audit.",
        "",
        "Volume comparisons are made only inside the same feed and timeframe. "
        "Average relative volume means each bar's volume divided by its trailing "
        "50-bar average. RSI values are aligned to the close of the bar containing "
        "the stated price pivot.",
        "",
        "## Replay Checkpoints",
        "",
        "| Cutoff | Visible pivot | What could be concluded then |",
        "|---|---:|---|",
    ]
    for item in audit["replay_checkpoints"]:
        lines.append(
            f"| {item['cutoff']} | ${item['pivot']:.3f} | {item['observation']} |"
        )
    lines.extend(
        [
            "",
            "## Competing Count 1: Primary A as One Outer Impulse",
            "",
            *motive_table(outer),
            "",
            "RSI and volume confirmation:",
            "",
            f"- Wave 3 RSI stronger than Wave 1: "
            f"{fmt(outer['rsi_volume_confirmation']['wave_3_rsi_stronger_than_wave_1'])}.",
            f"- Wave 3 average relative volume / Wave 1: "
            f"{fmt(outer['rsi_volume_confirmation']['wave_3_average_relative_volume_vs_wave_1'], 3)}.",
            f"- Wave 5 aligned RSI divergence against Wave 3: "
            f"{fmt(outer['rsi_volume_confirmation']['wave_5_aligned_endpoint_rsi_divergence_vs_wave_3'])}.",
            f"- Wave 5 relative-volume dry-up against Wave 3: "
            f"{fmt(outer['rsi_volume_confirmation']['wave_5_average_relative_volume_dry_up_vs_wave_3'])}.",
            "",
            *cross_timeframe_table(outer),
            "",
            "The price rules pass. One-hour RSI and volume show terminal divergence, "
            "but daily and 4-hour RSI do not agree. The outer impulse is valid, while "
            "its multi-timeframe confirmation remains mixed.",
            "",
            "## Competing Count 2: W as a 5-3-5 Zigzag",
            "",
            "### W-A",
            "",
            *motive_table(w_a),
            "",
            *cross_timeframe_table(w_a),
            "",
            "### W-B",
            "",
            "| Leg | Path | RSI end | Avg rel. volume |",
            "|---|---:|---:|---:|",
        ]
    )
    for wave in w_b["waves"]:
        lines.append(
            f"| {wave['label']} | ${wave['start_price']:.2f} -> "
            f"${wave['end_price']:.2f} | {fmt(wave['rsi_at_end_bar_close'])} | "
            f"{fmt(wave['average_relative_volume_50'], 3)} |"
        )
    lines.extend(
        [
            "",
            "### W-C",
            "",
            *motive_table(w_c),
            "",
            "RSI and volume confirmation:",
            "",
            f"- W-C Wave 3 RSI stronger than Wave 1: "
            f"{fmt(w_c['rsi_volume_confirmation']['wave_3_rsi_stronger_than_wave_1'])}.",
            f"- W-C Wave 5 aligned RSI divergence against Wave 3: "
            f"{fmt(w_c['rsi_volume_confirmation']['wave_5_aligned_endpoint_rsi_divergence_vs_wave_3'])}.",
            f"- W-C Wave 5 relative-volume dry-up against Wave 3: "
            f"{fmt(w_c['rsi_volume_confirmation']['wave_5_average_relative_volume_dry_up_vs_wave_3'])}.",
            "",
            *cross_timeframe_table(w_c),
            "",
            "This decomposition gives the first major decline a complete 5-3-5 "
            "footprint. Its 1-hour terminal signature is cleaner than the outer "
            "impulse count, although confirmation is still not unanimous on every "
            "timeframe.",
            "",
            "## Primary B or X: $356.26 to $466.33",
            "",
            "| Leg | Path | RSI end | Avg rel. volume |",
            "|---|---:|---:|---:|",
        ]
    )
    for wave in rebound["waves"]:
        lines.append(
            f"| {wave['label']} | ${wave['start_price']:.2f} -> "
            f"${wave['end_price']:.2f} | {fmt(wave['rsi_at_end_bar_close'])} | "
            f"{fmt(wave['average_relative_volume_50'], 3)} |"
        )
    lines.extend(
        [
            "",
            "Lower-timeframe subdivision:",
            "",
            f"- First leg: five waves from `${rebound_a['boundaries'][0]['price']:.2f}` "
            f"to `${rebound_a['boundaries'][-1]['price']:.2f}`.",
            f"- Middle leg: three waves from `${rebound_b['boundaries'][0]['price']:.2f}` "
            f"to `${rebound_b['boundaries'][-1]['price']:.2f}`.",
            f"- Final leg: an overlapping five from "
            f"`${rebound_c['boundaries'][0]['price']:.2f}` to "
            f"`${rebound_c['boundaries'][-1]['price']:.2f}`, consistent with a "
            "diagonal candidate rather than a standard impulse.",
            "",
            "The price footprint is corrective. RSI resets support that reading, "
            "while relative volume is broadly neutral rather than a decisive "
            "confirmation. Neither indicator can distinguish B from X.",
            "",
            "## Active Primary C or Y",
            "",
            "### Direct Primary C impulse: stronger degree interpretation",
            "",
            "| Intermediate wave | Path | RSI end | RSI extreme | Avg rel. volume |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for wave in direct_c["waves"]:
        lines.append(
            f"| {wave['label']} | ${wave['start_price']:.3f} -> "
            f"${wave['end_price']:.3f} | {fmt(wave['rsi_at_end_bar_close'])} | "
            f"{fmt(wave['rsi_extreme_in_wave_direction'])} | "
            f"{fmt(wave['average_relative_volume_50'], 3)} |"
        )
    lines.extend(
        [
            "",
            f"- Intermediate (3) is longer than (1): "
            f"{fmt(direct_c['partial_hard_price_rules']['wave_3_longer_than_wave_1'])}.",
            f"- Intermediate (4) avoids (1) territory: "
            f"{fmt(direct_c['partial_hard_price_rules']['wave_4_no_overlap'])}, "
            f"with `${direct_c['partial_hard_price_rules']['wave_4_overlap_clearance']:.3f}` "
            "clearance.",
            f"- Intermediate (3) has stronger downside RSI than (1): "
            f"{fmt(direct_c['rsi_volume_confirmation']['wave_3_rsi_stronger_than_wave_1'])}.",
            f"- Intermediate (4) average relative volume dries up versus (3): "
            f"{fmt(direct_c['rsi_volume_confirmation']['wave_4_volume_dry_up_vs_wave_3'])}.",
            f"- Intermediate (5) has made a new price low: "
            f"{fmt(direct_c['rsi_volume_confirmation']['wave_5_new_price_extreme'])}; "
            "RSI divergence is therefore not yet comparable.",
            "",
            "Inside Intermediate (3), the 30-minute chart supplies a price-valid "
            "Minor five with mixed RSI/volume confirmation:",
            "",
            *motive_table(direct_c_wave_3),
            "",
            *cross_timeframe_table(direct_c_wave_3),
            "",
            "This direct impulse interpretation explains the strong RSI and volume "
            "at $349.20 as a third-wave low instead of forcing it to be an exhausted "
            "fifth. The complex rebound to $405.975 then serves naturally as Wave "
            "(4), and the current decline is Wave (5).",
            "",
            "### W-X-Y alternate: Y-A to $349.20",
            "",
            *motive_table(y_a),
            "",
            *cross_timeframe_table(y_a),
            "",
            "This five passes the hard price rules. Wave 3 has the strongest downside "
            "RSI, but its average relative volume is lower than Wave 1 and Wave 5. "
            "RSI supports the impulse; volume confirmation is mixed.",
            "",
            "### Corrective rebound: $349.20 to $405.975",
            "",
            "| Leg | Path | RSI end | Avg rel. volume |",
            "|---|---:|---:|---:|",
        ]
    )
    for wave in y_b["waves"]:
        lines.append(
            f"| {wave['label']} | ${wave['start_price']:.2f} -> "
            f"${wave['end_price']:.3f} | {fmt(wave['rsi_at_end_bar_close'])} | "
            f"{fmt(wave['average_relative_volume_50'], 3)} |"
        )
    lines.extend(
        [
            "",
            "The rebound overlaps too deeply to be a standard impulse. A "
            "W-X-Y-X2-Z triple-three is a coherent working label. The final Z itself "
            "subdivides as:",
            "",
            "| Z subleg | Path | RSI end | Avg rel. volume |",
            "|---|---:|---:|---:|",
        ]
    )
    for wave in y_b_z["waves"]:
        lines.append(
            f"| {wave['label']} | ${wave['start_price']:.2f} -> "
            f"${wave['end_price']:.3f} | {fmt(wave['rsi_at_end_bar_close'])} | "
            f"{fmt(wave['average_relative_volume_50'], 3)} |"
        )
    lines.extend(
        [
            "",
            "The exact family remains provisional because several smaller "
            "combinations fit, and the daily $359.90 wick is absent from the 1-hour "
            "feed. The reliable conclusion is corrective, overlapping and complete "
            "at $405.975; the triple-three name is a working hypothesis.",
            "",
            "### Current decline from $405.975",
            "",
            "| Leg | Path | RSI end | RSI extreme | Avg rel. volume |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for wave in active["waves"]:
        lines.append(
            f"| {wave['label']} | ${wave['start_price']:.3f} -> "
            f"${wave['end_price']:.2f} | {fmt(wave['rsi_at_end_bar_close'])} | "
            f"{fmt(wave['rsi_extreme_in_wave_direction'])} | "
            f"{fmt(wave['average_relative_volume_50'], 3)} |"
        )
    lines.extend(
        [
            "",
            "The local third wave has stronger downside RSI than the first, but lower "
            "average relative volume. The candidate fifth has not broken $377.39, "
            "so it is not yet a confirmed fifth-wave low. This local impulse has "
            "mixed confirmation.",
            "",
            "## Data Corrections",
            "",
            "- The earlier path `$555.45 -> $526.83 -> $535.79` is rejected: "
            "30-minute data proves `$535.79` occurred before `$526.83`.",
            "- The corrected first five is `$555.45 -> $520.88 -> $538.25 -> "
            "$498.51 -> $510.90 -> $492.38`.",
            "- The daily candle reports `$359.90` on 29 June 2026, while the "
            "1-hour Cboe One series reports `$366.82`. These pivots are not silently "
            "mixed.",
            "",
            "## Verdict",
            "",
            "Both parent counts remain price-valid. The W interpretation gives the "
            "first major decline a cleaner lower-timeframe terminal signature. "
            "However, the direct five developing from $466.33 fits Primary C more "
            "cleanly than it fits a separate Y-A/Y-B/Y-C zigzag. On the complete "
            "evidence, A-B-C becomes the preferred working count again, while W-X-Y "
            "remains a serious unresolved alternate.",
            "",
            "Preferred working hierarchy:",
            "",
            "1. Cycle IV Primary A: `$555.45 -> $356.26`, complete price-valid five.",
            "2. Cycle IV Primary B: `$356.26 -> $466.33`, complete correction.",
            "3. Primary C Intermediate (1): `$466.33 -> $424.26`.",
            "4. Intermediate (2): `$424.26 -> $436.10`.",
            "5. Intermediate (3): `$436.10 -> $349.20`, complete five.",
            "6. Intermediate (4): `$349.20 -> $405.975`, complete complex correction.",
            "7. Intermediate (5): active from `$405.975`, not yet below `$349.20`.",
            "",
            "W-X-Y alternate:",
            "",
            "1. W: `$555.45 -> $356.26`, complete 5-3-5 zigzag.",
            "2. X: `$356.26 -> $466.33`, complete correction.",
            "3. Y-A: `$466.33 -> $349.20`, price-valid five with mixed RSI/volume.",
            "4. Y-B: `$349.20 -> $405.975`, complex correction.",
            "5. Y-C: active from `$405.975`.",
            "",
            "The immediate evidence boundary is unchanged: below `$377.39` supports "
            "the developing local fifth, while above `$389.00` and especially "
            "`$403.15-$405.975` weakens or invalidates that exact bearish subdivision.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-date", default=SNAPSHOT_DATE)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "msft_cycle_iv_rsi_volume_replay_20260725.json",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=ROOT / "MSFT_CYCLE_IV_RSI_VOLUME_REPLAY_2026-07-25.md",
    )
    args = parser.parse_args()
    audit = build_audit(args.snapshot_date)
    args.json_output.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    args.report_output.write_text(render_report(audit), encoding="utf-8")
    print(
        json.dumps(
            {
                "analysis_id": audit["analysis_id"],
                "json_output": str(args.json_output),
                "report_output": str(args.report_output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
