import json
import math
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_date(value):
    value = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", "", value)
    for fmt in ("%d %b '%y %H:%M", "%d %b '%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(value)


def parse_volume(value):
    cleaned = re.sub(r"[^0-9.KMB]", "", value.upper())
    match = re.fullmatch(r"([0-9.]+)([KMB]?)", cleaned)
    if not match:
        return 0.0
    return float(match.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2)]


def load(*names):
    merged = {}
    for name in names:
        for raw in json.loads((ROOT / name).read_text(encoding="utf-8")):
            try:
                timestamp = parse_date(raw["date"])
                merged[timestamp] = {
                    "timestamp": timestamp,
                    "open": float(raw["open"].replace(",", "")),
                    "high": float(raw["high"].replace(",", "")),
                    "low": float(raw["low"].replace(",", "")),
                    "close": float(raw["close"].replace(",", "")),
                    "volume": parse_volume(raw.get("volume", "")),
                }
            except (KeyError, TypeError, ValueError):
                continue
    rows = [merged[key] for key in sorted(merged)]
    add_indicators(rows)
    return rows


def ema(values, period):
    alpha = 2 / (period + 1)
    result = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def sma(values, period):
    result = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def add_indicators(rows):
    if not rows:
        return
    closes = [row["close"] for row in rows]
    medians = [(row["high"] + row["low"]) / 2 for row in rows]
    ema12, ema26 = ema(closes, 12), ema(closes, 26)
    macd = [fast - slow for fast, slow in zip(ema12, ema26)]
    signal = ema(macd, 9)
    fast_sma, slow_sma = sma(medians, 5), sma(medians, 35)
    for index, row in enumerate(rows):
        row["macd"] = macd[index]
        row["macd_hist"] = macd[index] - signal[index]
        row["macd_pct"] = 100 * row["macd"] / row["close"] if row["close"] else 0
        row["macd_hist_pct"] = 100 * row["macd_hist"] / row["close"] if row["close"] else 0
        row["ewo"] = None if fast_sma[index] is None or slow_sma[index] is None else fast_sma[index] - slow_sma[index]
        row["ewo_pct"] = None if row["ewo"] is None or medians[index] == 0 else 100 * row["ewo"] / medians[index]


def segment_metrics(rows, start, end, direction):
    selected = [row for row in rows if start <= row["timestamp"] <= end]
    if not selected:
        return {"bars": 0}
    directional_macd = [row["macd"] * direction for row in selected]
    directional_hist = [row["macd_hist"] * direction for row in selected]
    directional_macd_pct = [row["macd_pct"] * direction for row in selected]
    directional_hist_pct = [row["macd_hist_pct"] * direction for row in selected]
    directional_ewo = [row["ewo_pct"] * direction for row in selected if row["ewo_pct"] is not None]
    volume = sum(row["volume"] for row in selected)
    return {
        "bars": len(selected),
        "cumulative_volume": round(volume),
        "average_volume": round(volume / len(selected)),
        "directional_macd_peak": round(max(directional_macd), 4),
        "directional_macd_hist_peak": round(max(directional_hist), 4),
        "directional_normalized_macd_peak": round(max(directional_macd_pct), 4),
        "directional_normalized_macd_hist_peak": round(max(directional_hist_pct), 4),
        "directional_normalized_ewo_peak": round(max(directional_ewo), 4) if directional_ewo else None,
    }


def impulse_rules(points, direction):
    if direction == 1:
        lengths = [points[1] - points[0], points[3] - points[2], points[5] - points[4]]
        overlap_buffer = points[4] - points[1]
    else:
        lengths = [points[0] - points[1], points[2] - points[3], points[4] - points[5]]
        overlap_buffer = points[1] - points[4]
    return {
        "wave_lengths": [round(value, 4) for value in lengths],
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": overlap_buffer > 0,
        "overlap_buffer": round(overlap_buffer, 4),
        "mandatory_pass": lengths[1] > min(lengths[0], lengths[2]) and overlap_buffer > 0,
    }


def log_lengths(points):
    return [round(math.log(points[1] / points[0]), 3), round(math.log(points[3] / points[2]), 3), round(math.log(points[5] / points[4]), 3)]


weekly = load("tv_nflx_weekly_complete.json", "tv_nflx_weekly_fresh_20260718.json")
daily = load("tv_nflx_daily_fresh_20260718.json")
two_hour = load("tv_nflx_2h_fresh_20260718.json")
one_hour = load("tv_nflx_1h_fresh_20260718.json")
fifteen_minute = load("tv_nflx_15m_fresh_20260718.json")

cycle_points = [0.0346, 4.35, 0.7544, 70.10, 16.27, 134.12]
primary_1 = [0.0346, 0.5681, 0.1273, 1.83, 1.36, 4.35]
primary_3 = [0.7544, 6.54, 4.57, 42.32, 25.23, 70.10]
primary_5 = [16.27, 48.50, 35.21, 106.45, 85.39, 134.12]
primary_5_intermediate_3 = [35.21, 63.80, 55.22, 93.53, 83.44, 106.45]
primary_5_intermediate_5 = [85.39, 95.14, 91.95, 126.28, 118.06, 134.12]

cycle_2_a = [134.12, 114.47, 126.71, 103.81, 109.73, 75.01]
cycle_2_b = [75.01, 100.19, 90.69, 108.95]
cycle_2_c = [108.95, 90.02, 94.70, 70.86, 78.44, 65.08]
minor_3 = [94.70, 85.10, 91.48, 79.28, 81.93, 70.86]
minor_5 = [78.44, 75.72, 78.18, 72.52, 75.44, 65.08]
minute_5 = [75.44, 72.28, 75.06, 67.35, 68.40, 65.08]

cycle_2_a_length = 134.12 - 75.01
cycle_advance = 134.12 - 0.0346

report = {
    "symbol": "NASDAQ:NFLX",
    "analysis_date": "2026-07-18",
    "method": "Fresh blind recount from raw TradingView OHLCV; no prior NFLX wave labels or reports imported",
    "data": {
        "basis": "TradingView split-adjusted NASDAQ prices; intraday includes extended hours",
        "weekly": [len(weekly), weekly[0]["timestamp"].isoformat(), weekly[-1]["timestamp"].isoformat()],
        "daily": [len(daily), daily[0]["timestamp"].isoformat(), daily[-1]["timestamp"].isoformat()],
        "two_hour": [len(two_hour), two_hour[0]["timestamp"].isoformat(), two_hour[-1]["timestamp"].isoformat()],
        "one_hour": [len(one_hour), one_hour[0]["timestamp"].isoformat(), one_hour[-1]["timestamp"].isoformat()],
        "fifteen_minute": [len(fifteen_minute), fifteen_minute[0]["timestamp"].isoformat(), fifteen_minute[-1]["timestamp"].isoformat()],
        "last_regular_close": 68.95,
        "last_low": 65.08,
        "last_daily_volume": 142_030_000,
    },
    "preferred_hierarchy": {
        "Cycle_I_complete": {
            "span": ["2002-10-07", 0.0346, "2025-06-30", 134.12],
            "Primary": {"1": [0.0346, 4.35], "2": [4.35, 0.7544], "3": [0.7544, 70.10], "4": [70.10, 16.27], "5": [16.27, 134.12]},
            "rules": impulse_rules(cycle_points, 1),
            "log_actionary_lengths": log_lengths(cycle_points),
            "interpretation": "Primary 1 and 3 are nearly equal on log scale; Primary 5 is smaller and terminal. This is more proportional than treating the 2012-2021 rise as an enormous terminal fifth.",
        },
        "Primary_1_detail": {"Intermediate": primary_1, "rules": impulse_rules(primary_1, 1)},
        "Primary_3_detail": {"Intermediate": primary_3, "rules": impulse_rules(primary_3, 1), "note": "Intermediate 4 is a complex correction ending at 25.23 after the initial 23.12 low."},
        "Primary_5_detail": {
            "Intermediate": primary_5,
            "rules": impulse_rules(primary_5, 1),
            "Intermediate_3_Minor": primary_5_intermediate_3,
            "Intermediate_3_rules": impulse_rules(primary_5_intermediate_3, 1),
            "Intermediate_5_Minor": primary_5_intermediate_5,
            "Intermediate_5_rules": impulse_rules(primary_5_intermediate_5, 1),
        },
        "Cycle_II_active": {
            "Primary_A": {"Intermediate": cycle_2_a, "rules": impulse_rules(cycle_2_a, -1)},
            "Primary_B": {"Intermediate_abc": cycle_2_b, "family": "three-wave zigzag candidate"},
            "Primary_C": {
                "Intermediate": cycle_2_c,
                "rules": impulse_rules(cycle_2_c, -1),
                "Intermediate_3_Minor": minor_3,
                "Intermediate_3_rules": impulse_rules(minor_3, -1),
                "Intermediate_5_Minor": minor_5,
                "Intermediate_5_rules": impulse_rules(minor_5, -1),
                "Intermediate_5_Minor_5_Minute": minute_5,
                "Intermediate_5_Minor_5_rules": impulse_rules(minute_5, -1),
            },
        },
    },
    "fresh_indicator_audit": {
        "Cycle_I_Primary_1": segment_metrics(weekly, datetime(2002, 10, 7), datetime(2011, 7, 17), 1),
        "Cycle_I_Primary_3": segment_metrics(weekly, datetime(2012, 7, 30), datetime(2021, 11, 21), 1),
        "Cycle_I_Primary_5": segment_metrics(weekly, datetime(2022, 5, 9), datetime(2025, 7, 6), 1),
        "Cycle_II_Primary_C": segment_metrics(daily, datetime(2026, 4, 16), datetime(2026, 7, 17, 23, 59), -1),
        "Primary_C_Minor_5": segment_metrics(two_hour, datetime(2026, 7, 2), datetime(2026, 7, 17, 23, 59), -1),
        "Minor_5_Minute_5": segment_metrics(one_hour, datetime(2026, 7, 13), datetime(2026, 7, 17, 23, 59), -1),
        "Friday_reversal_attempt": segment_metrics(fifteen_minute, datetime(2026, 7, 17, 13, 30), datetime(2026, 7, 17, 23, 59), 1),
        "Friday_volume_vs_prior_20_day_average": 2.82,
        "interpretation": "Normalized MACD and EWO are supporting evidence only. The Friday selloff expanded volume and downside momentum. The rebound from 65.08 has not yet produced a clean, non-overlapping five-wave advance on 15-minute data, so the low remains provisional.",
    },
    "fibonacci": {
        "Cycle_I_retracement_50_pct": round(134.12 - 0.5 * cycle_advance, 2),
        "Cycle_I_retracement_61_8_pct": round(134.12 - 0.618 * cycle_advance, 2),
        "Primary_C_61_8_pct_of_A": round(108.95 - 0.618 * cycle_2_a_length, 2),
        "Primary_C_78_6_pct_of_A": round(108.95 - 0.786 * cycle_2_a_length, 2),
        "Primary_C_equality_with_A": round(108.95 - cycle_2_a_length, 2),
        "Minor_5_equality_with_Minor_1": round(78.44 - (108.95 - 90.02), 2),
        "Minor_5_1_618_of_Minor_1": round(78.44 - 1.618 * (108.95 - 90.02), 2),
    },
    "live_state": {
        "preferred": "Cycle II > Primary C > Intermediate 5 > Minor 5 may have completed at 65.08, but reversal confirmation is absent",
        "first_confirmation": "A clean five-wave rise above 69.49 followed by a three-wave pullback holding 65.08",
        "stronger_confirmation": "Recover 75.44; this breaks the origin of the final Minute decline",
        "primary_confirmation": "Recover 78.44, then 94.70; above 108.95 confirms Primary C ended",
        "bearish_continuation": "Below 65.08 keeps Minute 5 extending toward 62.49 and possibly 59.51",
        "hard_invalidation": "Above 134.12 invalidates Cycle II as an active correction",
    },
    "alternate": {
        "count": "Cycle I ended at 70.10 in 2021; Cycle II ended at 16.27; Cycle III began there",
        "status": "Price-valid but secondary",
        "why_secondary": "It makes the 2012-2021 advance a disproportionately extended terminal fifth. The preferred 2002-2025 count has much cleaner log equality between Primary 1 and Primary 3 and a smaller terminal Primary 5.",
        "separator": "A completed corrective structure above 16.27 followed by a sustained motive advance through 134.12 would strengthen this alternate; a break below 16.27 would eliminate it.",
    },
    "confidence": {
        "Cycle_I_five_wave_swing_structure": "high",
        "exact_Cycle_Primary_degree_names": "moderate",
        "Cycle_II_ABC_family": "moderate-high",
        "Primary_C_five_down_to_65_08": "moderate",
        "65_08_final_low": "low until reversal confirmation",
    },
}

(ROOT / "nflx_fresh_blind_recount_2026-07-18.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

md = f"""# NFLX Fresh Blind Elliott Wave Recount

**Symbol:** NASDAQ:NFLX  
**Data through:** 17 July 2026  
**Method:** Raw TradingView OHLCV only. No earlier NFLX count was imported.

## Preferred High-Degree Count

### Cycle I: $0.0346 to $134.12, complete

| Primary | Price path |
|---|---:|
| I | $0.0346 to $4.35 |
| II | $4.35 to $0.7544 |
| III | $0.7544 to $70.10 |
| IV | $70.10 to $16.27 |
| V | $16.27 to $134.12 |

The count passes both mandatory impulse rules. On a logarithmic chart, actionary lengths are {log_lengths(cycle_points)}: Primary I and III are nearly equal, while Primary V is smaller. This is the main reason it outranks the alternate count ending Cycle I in 2021.

### Internals of Cycle I

- **Primary I:** $0.0346 -> $0.5681 -> $0.1273 -> $1.83 -> $1.36 -> $4.35.
- **Primary III:** $0.7544 -> $6.54 -> $4.57 -> $42.32 -> $25.23 -> $70.10.
- **Primary V:** $16.27 -> $48.50 -> $35.21 -> $106.45 -> $85.39 -> $134.12.
- Inside Primary V, Intermediate (3): $35.21 -> $63.80 -> $55.22 -> $93.53 -> $83.44 -> $106.45.
- Inside Primary V, Intermediate (5): $85.39 -> $95.14 -> $91.95 -> $126.28 -> $118.06 -> $134.12.

Every listed motive sequence passes Wave 3-not-shortest and Wave 4 no-overlap rules. Older lower-degree internals cannot be responsibly pushed beneath Minor degree because the available full-history dataset is weekly.

## Cycle II: Active From $134.12

### Primary A: $134.12 to $75.01

Five waves: $134.12 -> $114.47 -> $126.71 -> $103.81 -> $109.73 -> $75.01.

### Primary B: $75.01 to $108.95

Three waves: $75.01 -> $100.19 -> $90.69 -> $108.95. This is a zigzag candidate; the exact corrective family is less certain than the three-wave footprint.

### Primary C: $108.95 to $65.08, possibly complete but unconfirmed

Intermediate waves: $108.95 -> $90.02 -> $94.70 -> $70.86 -> $78.44 -> $65.08.

- Intermediate (3) Minor waves: $94.70 -> $85.10 -> $91.48 -> $79.28 -> $81.93 -> $70.86.
- Intermediate (5) Minor waves: $78.44 -> $75.72 -> $78.18 -> $72.52 -> $75.44 -> $65.08.
- Inside Minor 5, Minute waves: $75.44 -> $72.28 -> $75.06 -> $67.35 -> $68.40 -> $65.08.

The latest three nested impulses pass the two hard price rules. The 15-minute data does not support another complete degree beneath Minute without forcing labels. Friday's rebound from $65.08 to $69.49 is not yet a clean five-wave rise. Therefore $65.08 is a **provisional pivot**, not a confirmed Cycle II low.

## Fibonacci and Decision Levels

- $67.08: 50% retracement of the entire Cycle I advance; already slightly exceeded.
- $65.08: current provisional low and immediate bearish trigger.
- $62.49: Primary C = 0.786 of Primary A.
- $59.51: Minor 5 = equality with Minor 1.
- $51.25: 61.8% retracement of Cycle I.
- $49.84: Primary C = Primary A.
- $69.49: first local resistance; exceeding it alone is not enough.
- $75.44: breaks the origin of the final Minute decline.
- $78.44: stronger evidence that Primary C has completed.
- $94.70 and $108.95: major structural confirmations.

## Verdict

The preferred map is **Cycle I complete at $134.12; Cycle II active or attempting to bottom in Primary C**. The decline now contains a valid five-wave hierarchy down to Minute degree, but Friday's volume at 2.82 times its prior 20-session average and the incomplete 15-minute reversal keep the low unconfirmed. Below $65.08 favors extension toward $62.49-$59.51. A trustworthy reversal requires five waves up, then a three-wave pullback that holds above $65.08.

The alternate that Cycle I ended at $70.10 and Cycle III began at $16.27 remains price-valid, but it is secondary because its proposed terminal fifth is much less proportional on log scale.
"""

(ROOT / "NFLX_Fresh_Blind_Recount_Report_2026-07-18.md").write_text(md, encoding="utf-8")
print(json.dumps(report, indent=2))
