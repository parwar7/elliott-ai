import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import simple_moving_average, wilder_rsi as canonical_wilder_rsi


DAILY_FILE = ROOT / "tv_dell_daily_2024_2026.json"
FOUR_HOUR_FILE = ROOT / "tv_dell_4h_fullperiod.json"
OUTPUT_FILE = ROOT / "dell_revised_count_verification.json"


def parse_volume(value):
    cleaned = value.replace("\u202f", "").replace(" ", "").replace(",", "")
    match = re.fullmatch(r"([0-9.]+)([KMB]?)", cleaned, re.IGNORECASE)
    if not match:
        return 0.0
    multiplier = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2).upper()]
    return float(match.group(1)) * multiplier


def load_rows(path, date_format):
    records = []
    for raw in json.loads(path.read_text(encoding="utf-8")):
        date_text = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", "", raw["date"])
        try:
            timestamp = datetime.strptime(date_text, date_format)
            record = {
                "timestamp": timestamp,
                "open": float(raw["open"].replace(",", "")),
                "high": float(raw["high"].replace(",", "")),
                "low": float(raw["low"].replace(",", "")),
                "close": float(raw["close"].replace(",", "")),
                "volume": parse_volume(raw["volume"]),
            }
        except (ValueError, KeyError):
            continue
        records.append(record)
    records.sort(key=lambda item: item["timestamp"])
    add_indicators(records)
    return records


def sma(values, period):
    return simple_moving_average(values, period)


def wilder_rsi(closes, period=14):
    return canonical_wilder_rsi(closes, period)


def add_indicators(records):
    median = [(row["high"] + row["low"]) / 2.0 for row in records]
    sma5 = sma(median, 5)
    sma35 = sma(median, 35)
    rsi = wilder_rsi([row["close"] for row in records])
    for index, row in enumerate(records):
        row["ewo"] = None if sma5[index] is None or sma35[index] is None else sma5[index] - sma35[index]
        row["rsi"] = rsi[index]


def segment(records, start, end):
    return [row for row in records if start <= row["timestamp"] <= end]


def metrics(records, start, end):
    rows = segment(records, start, end)
    ewo_values = [row["ewo"] for row in rows if row["ewo"] is not None]
    volume = sum(row["volume"] for row in rows)
    return {
        "bars": len(rows),
        "cumulative_volume": round(volume, 2),
        "average_volume": round(volume / len(rows), 2) if rows else None,
        "absolute_ewo_peak": round(max((abs(value) for value in ewo_values), default=0.0), 4),
        "ewo_min": round(min(ewo_values), 4) if ewo_values else None,
        "ewo_max": round(max(ewo_values), 4) if ewo_values else None,
        "end_rsi": round(rows[-1]["rsi"], 2) if rows and rows[-1]["rsi"] is not None else None,
    }


def dt(value):
    return datetime.fromisoformat(value)


def impulse_rule_check(prices):
    wave1 = prices[1] - prices[0]
    wave3 = prices[3] - prices[2]
    wave5 = prices[5] - prices[4]
    return {
        "wave_lengths": {"1": round(wave1, 2), "3": round(wave3, 2), "5": round(wave5, 2)},
        "wave_3_not_shortest": abs(wave3) > min(abs(wave1), abs(wave5)),
        "wave_4_no_overlap": prices[4] > prices[1],
    }


daily = load_rows(DAILY_FILE, "%d %b '%y")
four_hour = load_rows(FOUR_HOUR_FILE, "%d %b '%y %H:%M")

minor_1_prices = [64.84, 87.39, 79.02, 140.55, 116.41, 166.69]
minor_1_rules = impulse_rule_check(minor_1_prices)

minor_2_a = 166.83 - 115.69
minor_2_b_retracement = (141.28 - 115.69) / minor_2_a * 100.0
minor_2_c_ratio = (141.28 - 109.88) / minor_2_a
minor_2_c_0618_target = 141.28 - minor_2_a * 0.618

minute_i_prices = [109.88, 153.25, 137.07, 190.77, 176.45, 263.99]
minute_iii_prices = [227.27, 311.56, 298.62, 443.86, 402.35, 469.47]

report = {
    "symbol": "NYSE:DELL",
    "analysis_date": "2026-07-14",
    "data": {
        "daily_rows": len(daily),
        "daily_start": daily[0]["timestamp"].isoformat(),
        "daily_end": daily[-1]["timestamp"].isoformat(),
        "four_hour_rows": len(four_hour),
        "four_hour_start": four_hour[0]["timestamp"].isoformat(),
        "four_hour_end": four_hour[-1]["timestamp"].isoformat(),
        "source": "TradingView Table View; adjusted NYSE:DELL",
    },
    "revised_hierarchy": {
        "Primary_3": "active",
        "Intermediate_3": "active",
        "Minor_1": "probable complete",
        "Minor_2": "probable complete ABC zigzag",
        "Minor_3": "active",
        "current_position": "Minute iv of Minor 3",
        "future_expected": ["Minute v of Minor 3", "Minor 4", "Minor 5"],
    },
    "minor_1": {
        "submitted_prices": minor_1_prices,
        "rule_check": minor_1_rules,
        "endpoint_note": "The adjusted daily series made a marginal higher high of 166.83 on 2025-11-03 after 166.69 on 2025-10-29. The strict price extreme favors 166.83 as the completed Minor 1 endpoint unless lower-timeframe structure proves 166.69 ended the impulse.",
        "minute_metrics_daily": {
            "i": metrics(daily, dt("2025-04-07"), dt("2025-04-14T23:59:59")),
            "ii": metrics(daily, dt("2025-04-14"), dt("2025-04-21T23:59:59")),
            "iii": metrics(daily, dt("2025-04-21"), dt("2025-08-12T23:59:59")),
            "iv": metrics(daily, dt("2025-08-12"), dt("2025-09-02T23:59:59")),
            "v": metrics(daily, dt("2025-09-02"), dt("2025-11-03T23:59:59")),
        },
        "status": "Probable valid five-wave impulse; endpoint should be normalized to 166.83 pending a lower-timeframe endpoint audit.",
    },
    "minor_2": {
        "preferred_structure": "ABC zigzag",
        "pivots": {
            "start": ["2025-11-03", 166.83],
            "A": ["2025-11-21", 115.69],
            "B": ["2025-12-08", 141.28],
            "C": ["2026-01-21", 109.88],
        },
        "fibonacci": {
            "B_retracement_of_A_pct": round(minor_2_b_retracement, 2),
            "C_to_A_length_ratio": round(minor_2_c_ratio, 4),
            "C_0618_target": round(minor_2_c_0618_target, 2),
            "actual_C_low": 109.88,
        },
        "four_hour_internal_candidates": {
            "A_five_down": [166.95, 142.86, 149.31, 129.24, 135.66, 116.57],
            "C_five_down": [138.37, 122.02, 128.29, 116.17, 121.33, 110.80],
        },
        "metrics_daily": {
            "A": metrics(daily, dt("2025-11-03"), dt("2025-11-21T23:59:59")),
            "B": metrics(daily, dt("2025-11-21"), dt("2025-12-08T23:59:59")),
            "C": metrics(daily, dt("2025-12-08"), dt("2026-01-21T23:59:59")),
        },
        "status": "Probable regular ABC zigzag. Both A and C have valid five-wave candidates on 4-hour data, B retraced about 50% of A, and C terminated almost exactly at the 0.618 A projection.",
    },
    "minor_3": {
        "status": "Active impulse candidate",
        "minute_i": {
            "pivots": minute_i_prices,
            "candidate_dates": ["2026-01-21", "2026-03-02", "2026-03-10", "2026-04-13", "2026-04-15", "2026-05-08"],
            "rule_check": impulse_rule_check(minute_i_prices),
            "metrics_daily": metrics(daily, dt("2026-01-21"), dt("2026-05-08T23:59:59")),
            "status": "Probable five-wave impulse.",
        },
        "minute_ii": {
            "pivots": {
                "start": ["2026-05-08", 263.99],
                "A": ["2026-05-12", 228.50],
                "B": ["2026-05-14", 260.54],
                "C": ["2026-05-19", 227.27],
            },
            "B_retracement_of_A_pct": round((260.54 - 228.50) / (263.99 - 228.50) * 100.0, 2),
            "note": "The adjusted daily and 4-hour regular-session data show 227.27/227.31, not 226.64. A 226.64 print may come from another feed or lower-timeframe extended-hours data.",
            "status": "Probable regular Flat (3-3-5), not a zigzag, because B retraced about 90% of A and C marginally undercut A.",
        },
        "minute_iii": {
            "pivots": minute_iii_prices,
            "candidate_dates": ["2026-05-19", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29", "2026-06-01"],
            "rule_check": impulse_rule_check(minute_iii_prices),
            "metrics_four_hour": {
                "minuette_i": metrics(four_hour, dt("2026-05-19T08:00:00"), dt("2026-05-26T08:00:00")),
                "minuette_ii": metrics(four_hour, dt("2026-05-26T08:00:00"), dt("2026-05-27T12:00:00")),
                "minuette_iii": metrics(four_hour, dt("2026-05-27T12:00:00"), dt("2026-05-28T20:00:00")),
                "minuette_iv": metrics(four_hour, dt("2026-05-28T20:00:00"), dt("2026-05-29T12:00:00")),
                "minuette_v": metrics(four_hour, dt("2026-05-29T12:00:00"), dt("2026-06-01T20:00:00")),
            },
            "status": "Probable five-wave impulse with an extended Minuette iii. Minuette iv stayed above Minuette i territory.",
        },
        "minute_iv": {
            "observed_pivots": {
                "A_candidate": ["2026-06-09", 357.07],
                "B_candidate": ["2026-07-09", 460.50],
                "C_candidate": "developing/unconfirmed",
            },
            "B_retracement_of_A_pct": round((460.50 - 357.07) / (469.47 - 357.07) * 100.0, 2),
            "hard_no_overlap_level": 263.99,
            "status": "Active Flat candidate. Minute iv is not complete until a terminal C structure is confirmed; a move below 263.99 invalidates the standard Minor 3 impulse count.",
        },
    },
    "final_assessment": {
        "preferred_count": "Primary 3 > Intermediate (3) > Minor 3 > Minute iv active",
        "confidence": "probable",
        "changes_from_previous_report": "The 469.47 high is relabeled from completed Minor 3 to completed Minute iii inside an active Minor 3. The current correction is relabeled from Minor 4 to Minute iv. Minute v, Minor 4, and Minor 5 remain ahead.",
    },
}

OUTPUT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"output": str(OUTPUT_FILE), "assessment": report["final_assessment"]}, indent=2))
