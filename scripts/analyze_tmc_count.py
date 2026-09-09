import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import simple_moving_average, wilder_rsi


OUTPUT = ROOT / "tmc_elliott_wave_analysis.json"


def parse_volume(value):
    cleaned = value.replace("\u202f", "").replace(" ", "").replace(",", "")
    match = re.fullmatch(r"([0-9.]+)([KMB]?)", cleaned, re.IGNORECASE)
    if not match:
        return 0.0
    return float(match.group(1)) * {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2).upper()]


def parse_date(value):
    value = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", "", value)
    for fmt in ("%d %b '%y %H:%M", "%d %b '%y", "%b '%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(value)


def sma(values, period):
    return simple_moving_average(values, period)


def rsi_wilder(values, period=14):
    return wilder_rsi(values, period)


def load(path):
    rows = []
    for raw in json.loads(path.read_text(encoding="utf-8")):
        try:
            rows.append({
                "timestamp": parse_date(raw["date"]),
                "open": float(raw["open"].replace(",", "")),
                "high": float(raw["high"].replace(",", "")),
                "low": float(raw["low"].replace(",", "")),
                "close": float(raw["close"].replace(",", "")),
                "volume": parse_volume(raw["volume"]),
            })
        except (KeyError, ValueError):
            continue
    rows.sort(key=lambda row: row["timestamp"])
    medians = [(row["high"] + row["low"]) / 2 for row in rows]
    fast, slow = sma(medians, 5), sma(medians, 35)
    rsi = rsi_wilder([row["close"] for row in rows])
    for index, row in enumerate(rows):
        row["ewo"] = None if fast[index] is None or slow[index] is None else fast[index] - slow[index]
        row["rsi"] = rsi[index]
    return rows


def dt(value):
    return datetime.fromisoformat(value)


def metrics(rows, start, end):
    selected = [row for row in rows if start <= row["timestamp"] <= end]
    ewo = [row["ewo"] for row in selected if row["ewo"] is not None]
    rsi = [row["rsi"] for row in selected if row["rsi"] is not None]
    volume = sum(row["volume"] for row in selected)
    return {
        "bars": len(selected),
        "cumulative_volume": round(volume),
        "average_volume": round(volume / len(selected)) if selected else None,
        "ewo_peak_abs": round(max((abs(value) for value in ewo), default=0), 4),
        "ewo_min": round(min(ewo), 4) if ewo else None,
        "ewo_max": round(max(ewo), 4) if ewo else None,
        "ewo_zero_cross": bool(ewo and min(ewo) <= 0 <= max(ewo)),
        "rsi_min": round(min(rsi), 2) if rsi else None,
        "rsi_max": round(max(rsi), 2) if rsi else None,
        "end_rsi": round(selected[-1]["rsi"], 2) if selected and selected[-1]["rsi"] is not None else None,
    }


def price_rules(points, direction="up"):
    lengths = [abs(points[1] - points[0]), abs(points[3] - points[2]), abs(points[5] - points[4])]
    no_overlap = points[4] > points[1] if direction == "up" else points[4] < points[1]
    return {
        "wave_lengths": {"1": round(lengths[0], 3), "3": round(lengths[1], 3), "5": round(lengths[2], 3)},
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": no_overlap,
    }


weekly = load(ROOT / "tv_tmc_weekly.json")
daily = load(ROOT / "tv_tmc_daily.json")
four_hour = load(ROOT / "tv_tmc_4h.json")

int3_points = [0.721, 2.55, 1.57, 8.12, 4.37, 11.35]
int3_metrics = {
    "Minor_1": metrics(weekly, dt("2024-12-16"), dt("2025-02-23T23:59:59")),
    "Minor_2": metrics(weekly, dt("2025-02-17"), dt("2025-04-06T23:59:59")),
    "Minor_3": metrics(weekly, dt("2025-03-31"), dt("2025-06-29T23:59:59")),
    "Minor_4": metrics(weekly, dt("2025-06-23"), dt("2025-08-24T23:59:59")),
    "Minor_5": metrics(weekly, dt("2025-08-18"), dt("2025-10-19T23:59:59")),
}

correction_metrics = {
    "W": metrics(daily, dt("2025-10-13"), dt("2025-11-17T23:59:59")),
    "X": metrics(daily, dt("2025-11-17"), dt("2026-01-22T23:59:59")),
    "Y_active": metrics(daily, dt("2026-01-22"), dt("2026-07-14T23:59:59")),
    "Y_w": metrics(daily, dt("2026-01-22"), dt("2026-03-30T23:59:59")),
    "Y_x": metrics(daily, dt("2026-03-30"), dt("2026-06-02T23:59:59")),
    "Y_y_active": metrics(daily, dt("2026-06-02"), dt("2026-07-14T23:59:59")),
}


def nearest(rows, when):
    row = min(rows, key=lambda item: abs((item["timestamp"] - when).total_seconds()))
    return {
        "date": row["timestamp"].isoformat(),
        "low": row["low"],
        "close": row["close"],
        "ewo": round(row["ewo"], 4) if row["ewo"] is not None else None,
        "rsi": round(row["rsi"], 2) if row["rsi"] is not None else None,
        "volume": round(row["volume"]),
    }


report = {
    "symbol": "NASDAQ:TMC",
    "analysis_date": "2026-07-14",
    "data_provenance": {
        "source": "Live TradingView Table View, NASDAQ:TMC",
        "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
        "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
        "four_hour": {"rows": len(four_hour), "start": four_hour[0]["timestamp"].isoformat(), "end": four_hour[-1]["timestamp"].isoformat()},
    },
    "degree_limit": "TMC's listed history is too short to confirm a full Cycle/Supercycle structure. Primary is the highest defensible operational degree.",
    "preferred_hierarchy": {
        "Primary_I": {"status": "active", "anchor": ["2022-12-19", 0.511]},
        "Intermediate_1": {"start": ["2022-12-19", 0.511], "end": ["2023-07-10", 3.20], "status": "probable complete"},
        "Intermediate_2": {
            "start": ["2023-07-10", 3.20], "end": ["2024-12-16", 0.721],
            "retracement_pct": round((3.20 - 0.721) / (3.20 - 0.511) * 100, 2),
            "preferred_structure": "probable complex W-X-Y",
            "candidate_pivots": {"W": ["2023-10-23", 0.803], "X": ["2024-03-25", 2.07], "Y": ["2024-12-16", 0.721]},
            "status": "complete, internal labels probable",
        },
        "Intermediate_3": {
            "start": ["2024-12-16", 0.721], "end": ["2025-10-13", 11.35],
            "minor_pivots": [
                ["start", "2024-12-16", 0.721], ["1", "2025-02-18", 2.55],
                ["2", "2025-03-31", 1.57], ["3", "2025-06-23", 8.12],
                ["4", "2025-08-18", 4.37], ["5", "2025-10-13", 11.35],
            ],
            "price_rule_check": price_rules(int3_points),
            "indicator_metrics": int3_metrics,
            "status": "probable complete five-wave impulse",
        },
        "Intermediate_4": {
            "status": "active",
            "preferred_structure": "complex W-X-Y",
            "pivots": {
                "start": ["2025-10-13", 11.35], "W": ["2025-11-17", 4.75],
                "X": ["2026-01-22", 10.05], "Y_current_low": ["2026-07-08", 3.84],
            },
            "X_retracement_of_W_pct": round((10.05 - 4.75) / (11.35 - 4.75) * 100, 2),
            "retracement_of_Intermediate_3_pct": round((11.35 - 3.84) / (11.35 - 0.721) * 100, 2),
            "Y_internal_candidate": {
                "w": ["2026-03-30", 3.93], "x": ["2026-06-02", 6.64], "y": ["2026-07-08", 3.84, "active/unconfirmed"],
                "reason": "A bearish five-wave C candidate would suffer wave-four overlap; the visible 10.05-3.93-6.64-3.84 path is better treated as corrective W-X-Y until proven otherwise.",
            },
            "indicator_metrics": correction_metrics,
        },
        "Intermediate_5": {"status": "expected only after Intermediate (4) confirms complete"},
    },
    "current_divergence_test": {
        "first_low": nearest(daily, dt("2026-03-30")),
        "lower_low": nearest(daily, dt("2026-07-08")),
        "interpretation": "Price made a marginal lower low. Bullish momentum divergence exists only if RSI/EWO at the July low is less negative/stronger than at the March low.",
    },
    "decision_levels": {
        "early_reversal_evidence": "Sustained recovery above 4.78, then 5.22; stronger evidence above 6.64.",
        "confirmation": "A completed lower-degree five-wave advance followed by a three-wave pullback above 3.84; price recovery above 6.64 materially strengthens an Intermediate (4) low call.",
        "hard_standard_impulse_invalidation": 3.20,
        "distance_from_current_low_to_invalidation": round(3.84 - 3.20, 2),
        "equality_reference": {"simple_A_equals_C_target": 3.45, "note": "Reference only; preferred correction label is W-X-Y."},
    },
    "alternate_count": {
        "trigger": "A sustained break below 3.20 overlaps Intermediate (1) territory and invalidates the standard Primary I impulse count.",
        "interpretation": "The entire 0.511 to 11.35 advance would then be reconsidered as a completed corrective rise or as a diagonal-type structure requiring a full recount.",
    },
    "confidence": {
        "Primary_I_active": "moderate",
        "Intermediate_3_complete_impulse": "high",
        "Intermediate_4_WXY_active": "moderate-high",
        "July_2026_terminal_low": "low until reversal confirmation",
    },
}

# Resolve whether the marginal July lower low has indicator divergence.
march = report["current_divergence_test"]["first_low"]
july = report["current_divergence_test"]["lower_low"]
report["current_divergence_test"]["bullish_rsi_divergence"] = bool(july["low"] < march["low"] and july["rsi"] > march["rsi"])
report["current_divergence_test"]["bullish_ewo_divergence"] = bool(july["low"] < march["low"] and july["ewo"] > march["ewo"])

OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
