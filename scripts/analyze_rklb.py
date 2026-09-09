import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import simple_moving_average, wilder_rsi


OUTPUT = ROOT / "rklb_elliott_wave_analysis.superseded.json"


def parse_volume(value):
    cleaned = re.sub(r"[^0-9.KMB]", "", value.upper())
    match = re.fullmatch(r"([0-9.]+)([KMB]?)", cleaned)
    if not match:
        return 0.0
    return float(match.group(1)) * {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2)]


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


def nearest(rows, when):
    row = min(rows, key=lambda item: abs((item["timestamp"] - when).total_seconds()))
    return {
        "date": row["timestamp"].isoformat(),
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": round(row["volume"]),
        "ewo": round(row["ewo"], 4) if row["ewo"] is not None else None,
        "rsi": round(row["rsi"], 2) if row["rsi"] is not None else None,
    }


def price_rules(points):
    lengths = [points[1] - points[0], points[3] - points[2], points[5] - points[4]]
    return {
        "wave_lengths": {"1": round(lengths[0], 2), "3": round(lengths[1], 2), "5": round(lengths[2], 2)},
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": points[4] > points[1],
    }


def local_pivots(rows, start, window=4):
    selected = [row for row in rows if row["timestamp"] >= start]
    pivots = []
    for index in range(window, len(selected) - window):
        block = selected[index - window:index + window + 1]
        row = selected[index]
        if row["high"] == max(item["high"] for item in block):
            pivots.append(("H", row["timestamp"].isoformat(), row["high"]))
        if row["low"] == min(item["low"] for item in block):
            pivots.append(("L", row["timestamp"].isoformat(), row["low"]))
    return pivots


weekly = load(ROOT / "tv_rklb_weekly.json")
daily = load(ROOT / "tv_rklb_daily.json")
two_hour = load(ROOT / "tv_rklb_2h.json")

extended_intermediate_3_points = [14.71, 53.44, 38.26, 99.58, 56.13, 151.00]
extended_intermediate_3_metrics = {
    "Minor_1": metrics(weekly, dt("2025-04-07"), dt("2025-07-20T23:59:59")),
    "Minor_2": metrics(weekly, dt("2025-07-14"), dt("2025-08-24T23:59:59")),
    "Minor_3": metrics(weekly, dt("2025-08-18"), dt("2026-01-18T23:59:59")),
    "Minor_4": metrics(weekly, dt("2026-01-12"), dt("2026-04-05T23:59:59")),
    "Minor_5": metrics(weekly, dt("2026-03-30"), dt("2026-05-31T23:59:59")),
}

intermediate_4_metrics = {
    "A": metrics(daily, dt("2026-05-27"), dt("2026-06-25T23:59:59")),
    "B": metrics(daily, dt("2026-06-25"), dt("2026-07-01T23:59:59")),
    "C_candidate": metrics(daily, dt("2026-07-01"), dt("2026-07-15T23:59:59")),
}

c_subwave_points = [107.58, 97.91, 102.53, 80.51, 88.38, 75.45]
c_subwave_metrics = {
    "Minute_i": metrics(two_hour, dt("2026-07-01T16:00"), dt("2026-07-02T16:00")),
    "Minute_ii": metrics(two_hour, dt("2026-07-02T16:00"), dt("2026-07-06T08:00")),
    "Minute_iii": metrics(two_hour, dt("2026-07-06T08:00"), dt("2026-07-08T08:00")),
    "Minute_iv": metrics(two_hour, dt("2026-07-08T08:00"), dt("2026-07-09T08:00")),
    "Minute_v": metrics(two_hour, dt("2026-07-09T08:00"), dt("2026-07-13T22:00")),
}

report = {
    "symbol": "NASDAQ:RKLB",
    "analysis_time": "2026-07-15 09:35 Europe/London",
    "data_provenance": {
        "source": "Live TradingView Table View, NASDAQ:RKLB",
        "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
        "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
        "two_hour": {"rows": len(two_hour), "start": two_hour[0]["timestamp"].isoformat(), "end": two_hour[-1]["timestamp"].isoformat()},
    },
    "degree_limit": "Public history is too short to confirm Cycle degree. Primary is the highest defensible operational degree.",
    "preferred_count": {
        "pre_anchor_context": "The September 2021 SPAC-era high near $21.34 corrected to the April 2024 low at $3.47.",
        "Primary_1": {
            "start": ["2024-04-15", 3.47],
            "status": "active; Intermediate (4) is active or forming a low",
            "intermediate_waves": [
                ["(1)", "2024-04-15", 3.47, "2025-01-21", 33.34],
                ["(2)", "2025-01-21", 33.34, "2025-04-07", 14.71],
                ["(3)", "2025-04-07", 14.71, "2026-05-27", 151.00],
                ["(4)", "2026-05-27", 151.00, "active", 75.45],
                ["(5)", "future", None, "future", None],
            ],
            "Intermediate_3": {
                "status": "complete extended impulse",
                "minor_waves": [
                    ["1", 14.71, 53.44], ["2", 53.44, 38.26], ["3", 38.26, 99.58],
                    ["4", 99.58, 56.13], ["5", 56.13, 151.00],
                ],
                "price_rule_check": price_rules(extended_intermediate_3_points),
                "indicator_metrics": extended_intermediate_3_metrics,
                "fibonacci": {
                    "Minor_3_to_Minor_1_length_ratio": round((99.58 - 38.26) / (53.44 - 14.71), 3),
                    "Minor_5_to_Minor_3_length_ratio": round((151.00 - 56.13) / (99.58 - 38.26), 3),
                },
                "note": "Minor 4 stayed $2.69 above Minor 1 territory. Minor 5 is an extended fifth without classic EWO divergence.",
            },
            "Intermediate_4": {
            "status": "five-wave C candidate complete, but reversal confirmation absent",
            "preferred_structure": "ABC zigzag candidate",
            "pivots": {"A": ["2026-06-25", 80.00], "B": ["2026-07-01", 107.60], "C_current_low": ["2026-07-13", 75.60]},
            "B_retracement_of_A_pct": round((107.60 - 80.00) / (151.00 - 80.00) * 100, 2),
            "retracement_of_Intermediate_3_pct": round((151.00 - 75.60) / (151.00 - 14.71) * 100, 2),
            "indicator_metrics": intermediate_4_metrics,
            "targets": {"C_0.618_of_A": round(107.60 - 0.618 * (151.00 - 80.00), 2), "C_equals_A": round(107.60 - (151.00 - 80.00), 2)},
            "C_internal_count": {
                "pivots": [
                    ["start", "2026-07-01 16:00", 107.58], ["i", "2026-07-02 16:00", 97.91],
                    ["ii", "2026-07-06 08:00", 102.53], ["iii", "2026-07-08 08:00", 80.51],
                    ["iv", "2026-07-09 08:00", 88.38], ["v", "2026-07-13 22:00", 75.45],
                ],
                "price_rule_check": {
                    "wave_lengths": {"1": 9.67, "3": 22.02, "5": 12.93},
                    "wave_3_not_shortest": True,
                    "wave_4_no_overlap": c_subwave_points[4] < c_subwave_points[1],
                },
                "indicator_metrics": c_subwave_metrics,
                "interpretation": "The five-down structure permits an Intermediate (4) low, but does not confirm it. A five-up reversal and three-down hold above the low are still required.",
            },
            },
            "Intermediate_5": {"status": "expected after Intermediate (4) confirms complete", "confirmation": "A later sustained break above $151"},
        },
        "Primary_2": {"status": "not formed under the preferred count"},
    },
    "lower_degree_examples": {
        "inside_Intermediate_1": [
            ["Minor 1", 3.47, 5.84], ["Minor 2", 5.84, 4.20], ["Minor 3", 4.20, 28.05],
            ["Minor 4", 28.05, 21.87], ["Minor 5", 21.87, 33.34],
        ],
        "inside_Intermediate_3": [
            ["Minor 1", 14.71, 53.44], ["Minor 2", 53.44, 38.26], ["Minor 3", 38.26, 99.58],
            ["Minor 4", 99.58, 56.13], ["Minor 5", 56.13, 151.00],
        ],
        "current_C_two_hour_pivots": local_pivots(two_hour, dt("2026-07-01"), 4),
    },
    "current": nearest(two_hour, dt("2026-07-15T08:00")),
    "current_low": nearest(two_hour, dt("2026-07-13T22:00")),
    "decision": {
        "stance": "No confirmed entry; wait for reversal structure or breakdown confirmation.",
        "bullish_evidence": "A five-wave rise from $75.60, followed by a three-wave pullback that holds above $75.60; stronger confirmation above $86.50 and $92.30.",
        "bearish_evidence": "A sustained break below $75.60 keeps C active toward $63.72; equality projects $36.60 but is a low-confidence tail target.",
        "structural_levels": "$53.44 is internal support but not a hard invalidation. Intermediate (4) may retrace into Intermediate (3)'s Minor 1 territory. Only a sustained break below $33.34 overlaps Primary 1 Intermediate (1) and invalidates the standard Primary 1 impulse.",
    },
    "alternate_count": "Primary 1 completed at $151 and the current decline is Primary 2. It remains price-valid but is downgraded because its proposed Intermediate (3) does not subdivide cleanly and its supposed Intermediate (5) had the strongest weekly momentum.",
    "confidence": {"Primary_1_active": "moderate-high", "Intermediate_3_complete": "high", "Intermediate_4_low_in_place": "low-moderate until five-up/three-down confirmation"},
}

OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
