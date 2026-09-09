import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import simple_moving_average, wilder_rsi


def parse_volume(value):
    cleaned = re.sub(r"[^0-9.KMB]", "", value.upper())
    match = re.fullmatch(r"([0-9.]+)([KMB]?)", cleaned)
    if not match:
        return 0.0
    return float(match.group(1)) * {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2)]


def parse_date(value):
    value = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", "", value)
    for fmt in ("%d %b '%y %H:%M", "%d %b '%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(value)


def sma(values, period):
    return simple_moving_average(values, period)


def rsi_wilder(values, period=14):
    return wilder_rsi(values, period)


def load(name):
    rows = []
    for raw in json.loads((ROOT / name).read_text(encoding="utf-8")):
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


def impulse_rules(points, direction="up"):
    if direction == "up":
        lengths = [points[1] - points[0], points[3] - points[2], points[5] - points[4]]
        no_overlap = points[4] > points[1]
        overlap_buffer = points[4] - points[1]
    else:
        lengths = [points[0] - points[1], points[2] - points[3], points[4] - points[5]]
        no_overlap = points[4] < points[1]
        overlap_buffer = points[1] - points[4]
    wave_3_not_shortest = lengths[1] > min(lengths[0], lengths[2])
    return {
        "direction": direction,
        "wave_lengths": {"1": round(lengths[0], 4), "3": round(lengths[1], 4), "5": round(lengths[2], 4)},
        "wave_3_not_shortest": wave_3_not_shortest,
        "wave_4_no_overlap": no_overlap,
        "overlap_buffer": round(overlap_buffer, 4),
        "mandatory_pass": wave_3_not_shortest and no_overlap,
    }


def verify_impulse(points, wave_metrics, direction):
    w1, w2, w3, w4, w5 = (wave_metrics[str(index)] for index in range(1, 6))
    return {
        "hard_rules": impulse_rules(points, direction),
        "wave_3_strength": w3["cumulative_volume"] > w1["cumulative_volume"] and w3["ewo_peak_abs"] > w1["ewo_peak_abs"],
        "correction_volume_dry_up": w2["cumulative_volume"] < w1["cumulative_volume"] and w4["cumulative_volume"] < w3["cumulative_volume"],
        "wave_4_ewo_zero_cross": w4["ewo_zero_cross"],
        "strict_wave_5_divergence": w5["cumulative_volume"] < w3["cumulative_volume"] and w5["ewo_peak_abs"] < w3["ewo_peak_abs"],
    }


def directional_assessment(wave_metrics, direction):
    motives = [wave_metrics[str(index)] for index in (1, 3, 5)]
    if direction == "up":
        momentum = [max(item["ewo_max"] or 0, 0) for item in motives]
    else:
        momentum = [abs(min(item["ewo_min"] or 0, 0)) for item in motives]
    volume = [item["cumulative_volume"] for item in motives]
    wave_3_weakest = momentum[1] == min(momentum) or volume[1] == min(volume)
    return {
        "directional_ewo_peaks_1_3_5": [round(value, 4) for value in momentum],
        "cumulative_volume_1_3_5": volume,
        "wave_3_stronger_than_wave_1_directionally": momentum[1] > momentum[0],
        "wave_3_is_weakest_motive": wave_3_weakest,
        "status": "Invalid: Wave 3 cannot be weakest" if wave_3_weakest else (
            "Passed: directional Wave 3 strength" if momentum[1] > momentum[0] and volume[1] > volume[0]
            else "Warning: Wave 3 does not exceed Wave 1 on every strict metric"
        ),
        "note": "Directional EWO excludes opposite-direction oscillator energy inherited at the starting pivot; absolute EWO remains in the raw verification for auditability.",
    }


monthly = load("tv_nflx_monthly.json")
weekly = load("tv_nflx_weekly.json")
daily = load("tv_nflx_daily.json")
two_hour = load("tv_nflx_2h.json")

cycle_points = [0.0346, 3.87, 0.76, 69.10, 16.43, 134.12]
primary_5_points = [16.43, 45.65, 35.21, 106.45, 85.39, 134.12]
primary_5_metrics = {
    "1": metrics(weekly, dt("2022-06-13"), dt("2023-07-16T23:59:59")),
    "2": metrics(weekly, dt("2023-07-10"), dt("2023-10-15T23:59:59")),
    "3": metrics(weekly, dt("2023-10-09"), dt("2025-02-16T23:59:59")),
    "4": metrics(weekly, dt("2025-02-10"), dt("2025-04-06T23:59:59")),
    "5": metrics(weekly, dt("2025-03-31"), dt("2025-07-06T23:59:59")),
}

primary_a_points = [134.12, 114.47, 126.57, 103.81, 109.73, 75.23]
primary_a_metrics = {
    "1": metrics(daily, dt("2025-06-30"), dt("2025-08-05T23:59:59")),
    "2": metrics(daily, dt("2025-08-05"), dt("2025-09-09T23:59:59")),
    "3": metrics(daily, dt("2025-09-09"), dt("2025-11-21T23:59:59")),
    "4": metrics(daily, dt("2025-11-21"), dt("2025-12-02T23:59:59")),
    "5": metrics(daily, dt("2025-12-02"), dt("2026-02-12T23:59:59")),
}

primary_c_points = [108.95, 91.30, 94.22, 70.86, 78.44, 72.28]
primary_c_metrics = {
    "1": metrics(daily, dt("2026-04-16"), dt("2026-04-27T23:59:59")),
    "2": metrics(daily, dt("2026-04-27"), dt("2026-04-30T23:59:59")),
    "3": metrics(daily, dt("2026-04-30"), dt("2026-06-25T23:59:59")),
    "4": metrics(daily, dt("2026-06-25"), dt("2026-07-02T23:59:59")),
    "5": metrics(daily, dt("2026-07-02"), dt("2026-07-15T23:59:59")),
}

cycle_start, cycle_high = cycle_points[0], cycle_points[-1]
primary_a_length = 134.12 - 75.23
primary_b_a = 100.19 - 75.23

report = {
    "symbol": "NASDAQ:NFLX",
    "analysis_time": "2026-07-15 Europe/London",
    "price_basis": "TradingView split-adjusted NASDAQ history",
    "data_provenance": {
        "source": "Live TradingView Table View",
        "monthly": {"rows": len(monthly), "start": monthly[0]["timestamp"].isoformat(), "end": monthly[-1]["timestamp"].isoformat()},
        "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
        "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
        "two_hour": {"rows": len(two_hour), "start": two_hour[0]["timestamp"].isoformat(), "end": two_hour[-1]["timestamp"].isoformat()},
    },
    "preferred_count": {
        "Cycle_I_complete": {
            "span": ["2002-10", 0.0346, "2025-06-30", 134.12],
            "Primary_waves": {"1": [0.0346, 3.87], "2": [3.87, 0.76], "3": [0.76, 69.10], "4": [69.10, 16.43], "5": [16.43, 134.12]},
            "rules": impulse_rules(cycle_points, "up"),
            "note": "The full adjusted public history resolves into a valid five-wave advance. Exact Cycle naming is moderate confidence; the five-swing structure is high confidence.",
        },
        "Primary_5_detail": {
            "Intermediate_waves": {"1": [16.43, 45.65], "2": [45.65, 35.21], "3": [35.21, 106.45], "4": [106.45, 85.39], "5": [85.39, 134.12]},
            "rules": impulse_rules(primary_5_points, "up"),
            "metrics": primary_5_metrics,
            "verification": verify_impulse(primary_5_points, primary_5_metrics, "up"),
            "directional_assessment": directional_assessment(primary_5_metrics, "up"),
            "Intermediate_3_Minor_waves": [35.21, 63.80, 55.22, 93.53, 83.44, 106.45],
            "Intermediate_3_rules": impulse_rules([35.21, 63.80, 55.22, 93.53, 83.44, 106.45], "up"),
            "Intermediate_5_Minor_waves": [85.39, 99.34, 94.92, 126.28, 118.06, 134.12],
            "Intermediate_5_rules": impulse_rules([85.39, 99.34, 94.92, 126.28, 118.06, 134.12], "up"),
        },
        "Cycle_II_active": {
            "Primary_A": {
                "span": [134.12, 75.23],
                "Intermediate_waves": primary_a_points,
                "rules": impulse_rules(primary_a_points, "down"),
                "metrics": primary_a_metrics,
                "verification": verify_impulse(primary_a_points, primary_a_metrics, "down"),
                "directional_assessment": directional_assessment(primary_a_metrics, "down"),
            },
            "Primary_B": {
                "span": [75.23, 108.95],
                "structure": [75.23, 100.19, 90.69, 108.95],
                "classification": "ABC zigzag",
                "B_retracement_of_A_pct": round((100.19 - 90.69) / primary_b_a * 100, 2),
                "C_to_A_ratio": round((108.95 - 90.69) / primary_b_a, 3),
            },
            "Primary_C_active": {
                "span_so_far": [108.95, 70.86],
                "Intermediate_waves": {"1": [108.95, 91.30], "2": [91.30, 94.22], "3": [94.22, 70.86], "4": [70.86, 78.44], "5": [78.44, 72.28, "active/possibly attempting a low"]},
                "rules_so_far": impulse_rules(primary_c_points, "down"),
                "metrics": primary_c_metrics,
                "verification_so_far": verify_impulse(primary_c_points, primary_c_metrics, "down"),
                "directional_assessment_so_far": directional_assessment(primary_c_metrics, "down"),
                "Intermediate_5_Minute_waves": {"i": [78.44, 75.72], "ii": [75.72, 78.18], "iii": [78.18, 72.52], "iv": [72.52, 75.39], "v": [75.39, 72.28, "active/unconfirmed"]},
                "minute_rules_so_far": impulse_rules([78.44, 75.72, 78.18, 72.52, 75.39, 72.28], "down"),
            },
        },
    },
    "fibonacci": {
        "Primary_C_0_618_of_A": round(108.95 - 0.618 * primary_a_length, 2),
        "Primary_C_0_786_of_A": round(108.95 - 0.786 * primary_a_length, 2),
        "Primary_C_equality_A": round(108.95 - primary_a_length, 2),
        "Cycle_I_38_2_retracement": round(cycle_high - 0.382 * (cycle_high - cycle_start), 2),
        "Cycle_I_50_retracement": round(cycle_high - 0.5 * (cycle_high - cycle_start), 2),
        "Cycle_I_61_8_retracement": round(cycle_high - 0.618 * (cycle_high - cycle_start), 2),
        "Intermediate_5_0_618_of_wave_1": round(78.44 - 0.618 * (108.95 - 91.30), 2),
        "Intermediate_5_equality_wave_1": round(78.44 - (108.95 - 91.30), 2),
    },
    "current_verdict": {
        "preferred_state": "Cycle II > Primary C > Intermediate (5) > Minute v active or attempting a low",
        "current_reference": "2-hour low $72.28; last extracted 2-hour close $73.73",
        "primary_target_zone": "$67.08-$67.53",
        "secondary_target_zone": "$60.79-$62.66",
        "deep_target_zone": "$50.06-$51.24",
        "near_term_bearish_invalidation": "Above $78.44 invalidates the placement of Intermediate (4) as complete",
        "primary_c_invalidation": "Above $108.95 invalidates the active Primary C count",
        "cycle_correction_invalidation": "Above $134.12 invalidates the interpretation that Cycle II remains active",
        "bottom_confirmation": "A completed five down below/around the terminal zone, followed by five up and a three-wave pullback that holds the low. Initial evidence above $78.44; stronger above $94.22; major confirmation above $108.95.",
        "confidence": "High on the completed five-wave structure into $134.12; moderate on Cycle/Primary degree names; moderate-high on the active ABC correction; low on calling $72.28 the final low before reversal confirmation.",
    },
    "alternate": {
        "name": "Primary C may have ended at $70.86 on June 25",
        "status": "Valid but not preferred",
        "reason": "C reached 0.618 of A and produced an oversold daily RSI, but the bounce to $78.44 looks corrective and price has already turned down in a valid lower-degree impulse.",
        "confirmation_needed": "A decisive five-wave rise through $78.44, followed by a three-wave pullback that holds above $70.86.",
    },
    "indicator_caveat": {
        "absolute_ewo": "The strict absolute-EWO comparison warns on older waves because the first bars retain opposite-direction EWO from the preceding correction.",
        "directional_ewo": "Directional EWO shows Wave 3 stronger than Wave 1 in Primary 5, Primary A, and active Primary C.",
        "volume": "Split-adjusted volume across multi-year waves is not directly stationary; price rules and same-timeframe lower-degree subdivisions carry more weight.",
        "correction_family": "ABC remains preferred because the first decline has five valid price waves. W-X-Y is retained as a lower-confidence alternate if the oscillator warning proves structural rather than inherited-pivot contamination.",
    },
}

(ROOT / "nflx_elliott_wave_analysis.superseded.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

md = f"""# NFLX Elliott Wave Analysis - 15 July 2026

## Preferred Count

TradingView's split-adjusted NASDAQ history supports a completed five-wave **Cycle I** from **$0.0346 (October 2002)** to **$134.12 (30 June 2025)**. Its Primary pivots are **$3.87, $0.76, $69.10, $16.43, and $134.12**. Wave 3 is not the shortest and Primary 4 remains above Primary 1 territory.

The current preferred state is **Cycle II > Primary C > Intermediate (5) > Minute v active or attempting a low**.

## Completed Bull Structure

- Primary 5: $16.43 -> $45.65 -> $35.21 -> $106.45 -> $85.39 -> $134.12.
- Intermediate (3): $35.21 -> $63.80 -> $55.22 -> $93.53 -> $83.44 -> $106.45.
- Intermediate (5): $85.39 -> $99.34 -> $94.92 -> $126.28 -> $118.06 -> $134.12.

All three impulses pass the mandatory Wave 3 length and Wave 4 no-overlap rules.

The strict absolute-EWO layer gives a warning because early Wave 1 bars retain oscillator energy from the preceding correction. Directional EWO shows Wave 3 stronger than Wave 1, so this is treated as an indicator caveat rather than a price-rule invalidation. Long-horizon split-adjusted volume is also not stationary.

## Active Correction

- Primary A: $134.12 -> $114.47 -> $126.57 -> $103.81 -> $109.73 -> $75.23. This is a valid five-wave decline.
- Primary B: $75.23 -> $100.19 -> $90.69 -> $108.95. This is best classified as an ABC zigzag; B retraced {report['preferred_count']['Cycle_II_active']['Primary_B']['B_retracement_of_A_pct']}% of A and C reached {report['preferred_count']['Cycle_II_active']['Primary_B']['C_to_A_ratio']} of A.
- Primary C: $108.95 -> $91.30 -> $94.22 -> $70.86 -> $78.44 -> active. The current fifth wave has reached $72.28 but is not confirmed complete.

Inside the current Intermediate (5), the lower-degree count is **$78.44 -> $75.72 -> $78.18 -> $72.52 -> $75.39 -> $72.28/current**. It passes the price rules so far, but the last pivot remains provisional.

## Levels

- Preferred terminal confluence: **$67.08-$67.53**.
- Secondary extension zone: **$60.79-$62.66**.
- Deep equality/retracement zone: **$50.06-$51.24**.
- Above **$78.44** invalidates the current Intermediate (5) placement.
- Above **$108.95** invalidates the active Primary C count.
- Above **$134.12** invalidates the active Cycle II interpretation.

## Decision

The structure remains corrective-bearish while below $78.44. A durable bottom requires a completed five down, then five up, followed by a three-wave pullback that holds the low. A move above $78.44 is only initial evidence; $94.22 is stronger confirmation and $108.95 is the major structural level.

The alternate is that Primary C ended at $70.86. It remains possible because C reached 0.618 of A, but it is not preferred until the reversal sequence above is visible.

A lower-confidence structural alternate labels the correction W-X-Y instead of A-B-C. ABC remains preferred because the first decline subdivides into five price-valid waves; the complex label becomes more credible only if the absolute-EWO warning is confirmed by subsequent structure.
"""
(ROOT / "NFLX_Elliott_Wave_Report_2026-07-15.superseded.md").write_text(
    "> **Superseded:** This report used only 333 weekly bars beginning in 2020 and overpromoted the 2025 high to Cycle degree. See the canonical blind recount instead.\n\n" + md,
    encoding="utf-8",
)
print(json.dumps(report, indent=2))
