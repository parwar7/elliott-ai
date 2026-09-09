from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOT = ROOT / "market_scans" / "2026-08-25_ssys"
TIMEFRAMES = ("monthly", "weekly", "daily", "4h", "1h", "15m")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Anchor:
    date: str
    price: float


@dataclass(frozen=True)
class Leg:
    label: str
    start: Anchor
    end: Anchor
    direction: Literal["up", "down"]


def latest_snapshot() -> Path:
    candidates = sorted(
        path
        for path in SCAN_ROOT.glob("SSYS_*")
        if path.is_dir() and (path / "SSYS_manifest.json").exists()
    )
    if not candidates:
        raise FileNotFoundError("No completed SSYS snapshot is available.")
    return candidates[-1]


def load_rows(snapshot: Path, timeframe: str) -> list[dict[str, Any]]:
    payload = json.loads(
        (snapshot / f"SSYS_{timeframe}_closed.json").read_text(encoding="utf-8")
    )
    return list(payload["candles"])


def impulse_rules(prices: list[float], direction: Literal["up", "down"]) -> dict[str, Any]:
    if len(prices) != 6:
        raise ValueError("An impulse needs its origin and five child endpoints.")
    signed = 1.0 if direction == "up" else -1.0
    motive = [signed * (prices[index] - prices[index - 1]) for index in (1, 3, 5)]
    wave_2_origin_holds = signed * (prices[2] - prices[0]) > 0
    wave_4_no_overlap = signed * (prices[4] - prices[1]) > 0
    return {
        "motive_lengths": [round(value, 6) for value in motive],
        "wave_2_origin_holds": wave_2_origin_holds,
        "wave_3_not_shortest": motive[1] >= min(motive[0], motive[2]),
        "wave_4_no_overlap": wave_4_no_overlap,
        "passed": (
            all(value > 0 for value in motive)
            and wave_2_origin_holds
            and motive[1] >= min(motive[0], motive[2])
            and wave_4_no_overlap
        ),
    }


def contracting_diagonal_rules(
    prices: list[float], direction: Literal["up", "down"]
) -> dict[str, Any]:
    if len(prices) != 6:
        raise ValueError("A diagonal needs its origin and five child endpoints.")
    signed = 1.0 if direction == "up" else -1.0
    motive = [signed * (prices[index] - prices[index - 1]) for index in (1, 3, 5)]
    corrections = [
        -signed * (prices[index] - prices[index - 1]) for index in (2, 4)
    ]
    overlap = signed * (prices[4] - prices[1]) <= 0
    return {
        "motive_lengths": [round(value, 6) for value in motive],
        "corrective_lengths": [round(value, 6) for value in corrections],
        "wave_2_origin_holds": signed * (prices[2] - prices[0]) > 0,
        "wave_3_not_shortest": motive[1] >= min(motive[0], motive[2]),
        "wave_4_overlap_present": overlap,
        "motive_sides_contract": motive[0] > motive[1] > motive[2],
        "corrective_sides_contract": corrections[0] > corrections[1],
        "passed_price_geometry": (
            all(value > 0 for value in motive + corrections)
            and signed * (prices[2] - prices[0]) > 0
            and motive[1] >= min(motive[0], motive[2])
            and overlap
            and motive[0] > motive[1] > motive[2]
            and corrections[0] > corrections[1]
        ),
        "family_proof_limit": (
            "Price geometry passes, but every 1, 3 and 5 leg must still retain "
            "a corrective three-wave footprint on its proving timeframe."
        ),
    }


def segment_evidence(
    rows: list[dict[str, Any]],
    leg: Leg,
    *,
    quarantined_volume_dates: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if leg.start.date <= row["date"][:10] <= leg.end.date
    ]
    if not selected:
        return {"availability": "not_covered"}
    rsi = [float(row["rsi14"]) for row in selected if row.get("rsi14") is not None]
    ewo = [float(row["ewo"]) for row in selected if row.get("ewo") is not None]
    volume_rows = [
        row for row in selected if row["date"][:10] not in quarantined_volume_dates
    ]
    quarantined_volume_rows = len(selected) - len(volume_rows)
    relative_volume = [
        float(row["volume_ratio20"])
        for row in volume_rows
        if row.get("volume_ratio20") is not None
    ]
    directional_extreme = max if leg.direction == "up" else min
    return {
        "availability": "available",
        "bars": len(selected),
        "rsi_extreme": round(directional_extreme(rsi), 6) if rsi else None,
        "rsi_end": round(rsi[-1], 6) if rsi else None,
        "ewo_extreme": round(directional_extreme(ewo), 6) if ewo else None,
        "ewo_end": round(ewo[-1], 6) if ewo else None,
        "volume_availability": (
            "available_excluding_quarantined_session"
            if quarantined_volume_rows
            else "available"
        ),
        "quarantined_volume_rows": quarantined_volume_rows,
        "average_relative_volume20": (
            round(sum(relative_volume) / len(relative_volume), 6)
            if relative_volume
            else None
        ),
        "average_volume": (
            round(
                sum(float(row["volume"]) for row in volume_rows)
                / len(volume_rows),
                6,
            )
            if volume_rows
            else None
        ),
    }


def wave_evidence(
    rows_by_timeframe: dict[str, list[dict[str, Any]]],
    legs: list[Leg],
    *,
    quarantined_volume_dates_by_timeframe: dict[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    quarantined = quarantined_volume_dates_by_timeframe or {}
    return {
        timeframe: {
            leg.label: segment_evidence(
                rows,
                leg,
                quarantined_volume_dates=quarantined.get(timeframe, frozenset()),
            )
            for leg in legs
        }
        for timeframe, rows in rows_by_timeframe.items()
    }


def build_audit(snapshot: Path) -> dict[str, Any]:
    rows = {timeframe: load_rows(snapshot, timeframe) for timeframe in TIMEFRAMES}

    major_advance_a = [0.625, 3.39333, 1.31667, 19.36667, 8.15, 31.45]
    major_advance_c = [7.58, 55.66, 17.88, 113.49, 89.90, 138.10]
    major_decline_a = [138.10, 85.30, 130.83, 33.85, 39.45, 14.48]
    major_decline_c = [56.95, 17.82, 42.83, 11.04, 21.72, 6.05]
    recovery_candidate = [6.05, 8.45, 6.92, 12.88, 8.47, 12.81]
    current_a = [10.79, 8.31, 9.35, 7.985, 8.95, 7.59]
    current_c = [9.32, 8.27, 9.195, 7.755, 8.015, 7.60]

    recovery_legs = [
        Leg("1", Anchor("2024-08-29", 6.05), Anchor("2024-09-30", 8.45), "up"),
        Leg("2", Anchor("2024-09-30", 8.45), Anchor("2024-10-25", 6.92), "down"),
        Leg("3", Anchor("2024-10-25", 6.92), Anchor("2025-02-06", 12.88), "up"),
        Leg("4", Anchor("2025-02-06", 12.88), Anchor("2025-04-07", 8.47), "down"),
        Leg("5", Anchor("2025-04-07", 8.47), Anchor("2025-10-21", 12.81), "up"),
    ]
    current_c_legs = [
        Leg("1", Anchor("2026-08-13", 9.32), Anchor("2026-08-13", 8.27), "down"),
        Leg("2", Anchor("2026-08-13", 8.27), Anchor("2026-08-13", 9.195), "up"),
        Leg("3", Anchor("2026-08-13", 9.195), Anchor("2026-08-20", 7.755), "down"),
        Leg("4", Anchor("2026-08-20", 7.755), Anchor("2026-08-21", 8.015), "up"),
        Leg("5", Anchor("2026-08-21", 8.015), Anchor("2026-08-25", 7.60), "down"),
    ]

    decline_a_log = math.log(138.10 / 14.48)
    decline_c_log = math.log(56.95 / 6.05)
    current_a_length = 10.79 - 7.59
    current_b_end = 9.32
    current_first_down = 9.32 - 7.60

    audit: dict[str, Any] = {
        "analysis_id": "ssys-top-down-price-first-20260825-v2",
        "symbol": "NASDAQ:SSYS",
        "source_snapshot": snapshot.name,
        "data_cutoffs": {
            timeframe: values[-1]["date"] for timeframe, values in rows.items()
        },
        "method": {
            "macro_scale": "logarithmic price relationships",
            "active_scale": "arithmetic Fibonacci relationships",
            "hard_rules": "price structure before Fibonacci or indicators",
            "soft_evidence": "Wilder RSI(14), EWO(5,35), same-feed relative volume and duration",
            "status_policy": "question-mark labels remain candidates, not verified facts",
        },
        "preferred_hierarchy": {
            "visible_pre_origin_correction": {
                "structure": "A-B-C candidate",
                "anchors": [["1996-09-23", 8.41667], ["1999-04-19", 1.08333], ["2000-02-14", 4.33333], ["2000-12-18", 0.625]],
                "degree": "unresolved because the chart begins mid-history",
            },
            "major_advance_2000_2014": {
                "structure": "5-3-5 zigzag",
                "A": ["2000-12-18", 0.625, "2007-10-29", 31.45],
                "B": ["2007-10-29", 31.45, "2009-03-30", 7.58],
                "C": ["2009-03-30", 7.58, "2013-12-30", 138.10],
                "reason_not_cycle_1_2_3": "That label would make the later 2014-2024 decline overlap the prior Wave 1 territory.",
            },
            "major_decline_2014_2024": {
                "structure": "5-3-5 zigzag candidate",
                "A": ["2013-12-30", 138.10, "2016-02-08", 14.48],
                "B": ["2016-02-08", 14.48, "2021-02-08", 56.95, "W-X-Y-X2-Z"],
                "C": ["2021-02-08", 56.95, "2024-08-29", 6.05, "contracting ending diagonal"],
                "status": "price-complete candidate; the post-low motive reversal is not yet proven",
            },
            "post_2024_primary": {
                "bullish_candidate": {
                    "wave_1": ["2024-08-29", 6.05, "2025-10-21", 12.81, "truncated-fifth impulse candidate"],
                    "wave_2": ["2025-10-21", 12.81, "2026-03-30", 7.34, "W-X-Y"],
                    "wave_3_intermediate_1": ["2026-03-30", 7.34, "2026-06-01", 10.79, "motive candidate"],
                    "wave_3_intermediate_2": ["2026-06-01", 10.79, "active", 7.60],
                    "hard_invalidation": 7.34,
                },
                "bearish_alternative": "The entire rebound from 6.05 is corrective B/X and the larger decline remains unfinished.",
            },
            "active_correction": {
                "preferred_family": "A-B-C zigzag candidate",
                "A": ["2026-06-01", 10.79, "2026-07-29", 7.59, "contracting leading diagonal candidate"],
                "B": ["2026-07-29", 7.59, "2026-08-13", 9.32, "corrective bull-trap rebound"],
                "C": ["2026-08-13", 9.32, "active", 7.60, "five-down candidate"],
                "complex_alternative": (
                    "W-X-Y remains unresolved only if the first decline and current "
                    "decline can be regrouped into corrective families on lower data."
                ),
            },
        },
        "deterministic_price_checks": {
            "major_advance_A_impulse": impulse_rules(major_advance_a, "up"),
            "major_advance_C_impulse": impulse_rules(major_advance_c, "up"),
            "major_decline_A_impulse": impulse_rules(major_decline_a, "down"),
            "major_decline_C_diagonal": contracting_diagonal_rules(major_decline_c, "down"),
            "post_2024_wave_1_candidate": {
                **impulse_rules(recovery_candidate, "up"),
                "truncated_fifth": recovery_candidate[-1] < recovery_candidate[3],
                "wave_4_overlap_margin": round(recovery_candidate[4] - recovery_candidate[1], 6),
            },
            "active_A_diagonal": contracting_diagonal_rules(current_a, "down"),
            "active_C_five_down_candidate": {
                **impulse_rules(current_c, "down"),
                "verification_status": "unproven",
                "reason_codes": [
                    "WAVE_1_HIGH_LOW_SHARE_ONE_15M_CANDLE",
                    "INTRABAR_PIVOT_ORDER_UNAVAILABLE",
                    "WAVE_5_TERMINATION_NOT_CONFIRMED",
                ],
            },
        },
        "fibonacci": {
            "major_decline_log": {
                "A_log_length": round(decline_a_log, 6),
                "B_log_retracement": round(math.log(56.95 / 14.48) / decline_a_log, 6),
                "C_log_length": round(decline_c_log, 6),
                "C_to_A_log_ratio": round(decline_c_log / decline_a_log, 6),
            },
            "active_C_down_targets": {
                "0.618_A_from_B": round(current_b_end - 0.618 * current_a_length, 4),
                "1.000_A_from_B": round(current_b_end - current_a_length, 4),
                "1.272_A_from_B": round(current_b_end - 1.272 * current_a_length, 4),
            },
            "first_rebound_after_7_60": {
                "0.236": round(7.60 + 0.236 * current_first_down, 4),
                "0.382": round(7.60 + 0.382 * current_first_down, 4),
                "0.500": round(7.60 + 0.500 * current_first_down, 4),
                "0.618": round(7.60 + 0.618 * current_first_down, 4),
                "0.786": round(7.60 + 0.786 * current_first_down, 4),
            },
            "active_wave_5_down_targets": {
                "0.382_wave_3_from_wave_4": round(8.015 - 0.382 * (9.195 - 7.755), 4),
                "0.618_wave_1_from_wave_4": round(8.015 - 0.618 * (9.32 - 8.27), 4),
                "wave_5_equals_wave_1": round(8.015 - (9.32 - 8.27), 4),
            },
        },
        "soft_evidence": {
            "post_2024_wave_1_candidate": wave_evidence(
                {key: rows[key] for key in ("weekly", "daily", "4h", "1h")},
                recovery_legs,
            ),
            "active_C_five_down_candidate": wave_evidence(
                {key: rows[key] for key in ("daily", "4h", "1h", "15m")},
                current_c_legs,
                quarantined_volume_dates_by_timeframe={
                    key: frozenset({"2026-08-25"})
                    for key in ("daily", "4h", "1h", "15m")
                },
            ),
            "current_intraday_volume_warning": (
                "The completed 2026-08-25 Twelve Data daily and intraday volume "
                "is anomalously low and quarantined from wave comparisons. Price, "
                "RSI and EWO remain available; volume evidence uses only earlier "
                "same-feed completed sessions."
            ),
            "pivot_aligned_wave_3_wave_5_divergence": {
                "15m": {
                    "wave_3": {"price": 7.755, "rsi14": 23.967051, "ewo": -0.448363},
                    "wave_5_candidate": {"price": 7.60, "rsi14": 35.49012, "ewo": -0.050147},
                    "status": "supportive_bullish_divergence",
                    "volume": "unavailable_current_session_quarantined",
                },
                "1h": {
                    "wave_3": {"price": 7.755, "rsi14": 17.79217, "ewo": -0.616957},
                    "wave_5_candidate": {"price": 7.60, "rsi14": 23.571242, "ewo": -0.473356},
                    "status": "supportive_bullish_divergence",
                    "volume": "unavailable_current_session_quarantined",
                },
                "4h": {
                    "wave_3": {"price": 7.755, "rsi14": 28.972332, "ewo": -0.159353},
                    "wave_5_candidate": {"price": 7.60, "rsi14": 32.979778, "ewo": -0.77111},
                    "status": "mixed_rsi_supportive_ewo_contradictory",
                    "volume": "unavailable_current_session_quarantined",
                },
            },
        },
        "current_decision_tree": {
            "immediate_zone": [7.47, 7.34],
            "terminal_case": "A completed five up, reclaim of 8.015, a higher low, and then recovery through 8.80 and 9.32 supports completion of C.",
            "extended_case": "A corrective rebound below 8.80 followed by a completed break of 7.34 supports continuation toward 6.965 and 6.12-6.05.",
            "bullish_parent_invalidation": 7.34,
            "active_decline_invalidation": 9.32,
            "major_low_invalidation": 6.05,
        },
    }
    audit["content_hash"] = canonical_hash(audit)
    return audit


def main() -> None:
    snapshot = latest_snapshot()
    audit = build_audit(snapshot)
    output = snapshot / "SSYS_top_down_audit.json"
    output.write_text(
        json.dumps(audit, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "content_hash": audit["content_hash"],
                "source_snapshot": snapshot.name,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
