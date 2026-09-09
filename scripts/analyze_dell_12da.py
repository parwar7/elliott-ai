import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.indicators import simple_moving_average, wilder_rsi as canonical_wilder_rsi


OUTPUT = ROOT / "dell_12da_gettex_analysis.json"


def parse_volume(value):
    cleaned = value.replace("\u202f", "").replace(" ", "").replace(",", "")
    match = re.fullmatch(r"([0-9.]+)([KMB]?)", cleaned, re.IGNORECASE)
    if not match:
        return 0.0
    return float(match.group(1)) * {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2).upper()]


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


def wilder_rsi(values, period=14):
    return canonical_wilder_rsi(values, period)


def load(filename):
    rows = []
    for raw in json.loads((ROOT / filename).read_text(encoding="utf-8")):
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
    median = [(row["high"] + row["low"]) / 2 for row in rows]
    fast, slow = sma(median, 5), sma(median, 35)
    rsi = wilder_rsi([row["close"] for row in rows])
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
        "average_volume": round(volume / len(selected), 2) if selected else None,
        "ewo_peak_abs": round(max((abs(value) for value in ewo), default=0), 4),
        "ewo_min": round(min(ewo), 4) if ewo else None,
        "ewo_max": round(max(ewo), 4) if ewo else None,
        "ewo_zero_cross": bool(ewo and min(ewo) <= 0 <= max(ewo)),
        "rsi_min": round(min(rsi), 2) if rsi else None,
        "rsi_max": round(max(rsi), 2) if rsi else None,
        "end_rsi": round(selected[-1]["rsi"], 2) if selected and selected[-1]["rsi"] is not None else None,
    }


def impulse_rules(points):
    lengths = [points[1] - points[0], points[3] - points[2], points[5] - points[4]]
    return {
        "wave_lengths": {"1": round(lengths[0], 2), "3": round(lengths[1], 2), "5": round(lengths[2], 2)},
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": points[4] > points[1],
    }


weekly = load("tv_12da_weekly.json")
daily = load("tv_12da_daily.json")
two_hour = load("tv_12da_2h.json")

int1_points = [31.70, 64.74, 57.90, 116.60, 92.97, 166.38]
minor1_points = [57.80, 77.42, 69.65, 120.62, 99.50, 147.51]
minute3_points = [196.66, 268.15, 256.50, 388.15, 345.55, 415.25]

minute4_a_length = 415.25 - 309.75
minute4_b_retrace = (402.35 - 309.75) / minute4_a_length * 100

report = {
    "symbol": "GETTEX:12DA",
    "currency": "EUR",
    "analysis_date": "2026-07-14",
    "last_observed": {"price": 393.35, "timestamp": "2026-07-14T15:00:00", "provisional_live_bar": True},
    "source": {
        "platform": "TradingView Table View",
        "monthly_rows": 125,
        "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
        "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
        "two_hour": {"rows": len(two_hour), "start": two_hour[0]["timestamp"].isoformat(), "end": two_hour[-1]["timestamp"].isoformat()},
        "warning": "GETTEX volume is venue-only and thin. Pre-November 2021 prices contain a corporate-action discontinuity, so the raw series is unsuitable for an uninterrupted high-degree wave count without normalization.",
    },
    "preferred_hierarchy": {
        "Cycle_I": "active, inherited from the corporate-action-normalized master count",
        "Primary_1": "complete on normalized master series; raw GETTEX history cannot independently verify it",
        "Primary_2": {
            "start": ["2022-02", 49.03], "end": ["2022-10-10", 31.70],
            "preferred_structure": "ABC zigzag candidate",
            "candidate_pivots": {"A": ["2022-05", 33.45], "B": ["2022-08-15", 44.78], "C": ["2022-10-10", 31.70]},
            "B_retracement_pct": round((44.78 - 33.45) / (49.03 - 33.45) * 100, 2),
            "C_to_A_ratio": round((44.78 - 31.70) / (49.03 - 33.45), 4),
            "status": "probable complete; exact lower-degree internals limited by thin historical data",
        },
        "Primary_3": {
            "start": ["2022-10-10", 31.70], "status": "active",
            "Intermediate_1": {
                "start": ["2022-10-10", 31.70], "end": ["2024-05-27", 166.38],
                "minor_pivots": [31.70, 64.74, 57.90, 116.60, 92.97, 166.38],
                "price_rules": impulse_rules(int1_points),
                "metrics": {
                    "1": metrics(weekly, dt("2022-10-10"), dt("2023-09-11T23:59:59")),
                    "2": metrics(weekly, dt("2023-09-11"), dt("2023-10-02T23:59:59")),
                    "3": metrics(weekly, dt("2023-10-02"), dt("2024-02-26T23:59:59")),
                    "4": metrics(weekly, dt("2024-02-26"), dt("2024-03-18T23:59:59")),
                    "5": metrics(weekly, dt("2024-03-18"), dt("2024-05-27T23:59:59")),
                },
                "status": "probable completed five-wave impulse",
            },
            "Intermediate_2": {
                "start": ["2024-05-27", 166.38], "end": ["2025-04-07", 57.80],
                "preferred_structure": "regular ABC zigzag",
                "pivots": {"A": ["2024-08-05", 77.34], "B": ["2024-11-25", 137.48], "C": ["2025-04-07", 57.80]},
                "B_retracement_pct": round((137.48 - 77.34) / (166.38 - 77.34) * 100, 2),
                "C_to_A_ratio": round((137.48 - 57.80) / (166.38 - 77.34), 4),
                "total_retracement_pct": round((166.38 - 57.80) / (166.38 - 31.70) * 100, 2),
                "metrics": {
                    "A": metrics(weekly, dt("2024-05-27"), dt("2024-08-05T23:59:59")),
                    "B": metrics(weekly, dt("2024-08-05"), dt("2024-11-25T23:59:59")),
                    "C": metrics(weekly, dt("2024-11-25"), dt("2025-04-07T23:59:59")),
                },
                "status": "probable complete",
            },
            "Intermediate_3": {
                "start": ["2025-04-07", 57.80], "status": "active",
                "Minor_1": {
                    "start": ["2025-04-07", 57.80], "end": ["2025-11-03", 147.51],
                    "minute_pivots": [57.80, 77.42, 69.65, 120.62, 99.50, 147.51],
                    "price_rules": impulse_rules(minor1_points),
                    "metrics": {
                        "i": metrics(daily, dt("2025-04-07"), dt("2025-04-14T23:59:59")),
                        "ii": metrics(daily, dt("2025-04-14"), dt("2025-04-22T23:59:59")),
                        "iii": metrics(daily, dt("2025-04-22"), dt("2025-08-13T23:59:59")),
                        "iv": metrics(daily, dt("2025-08-13"), dt("2025-09-09T23:59:59")),
                        "v": metrics(daily, dt("2025-09-09"), dt("2025-11-03T23:59:59")),
                    },
                    "status": "probable complete five-wave impulse",
                },
                "Minor_2": {
                    "start": ["2025-11-03", 147.51], "end": ["2026-02-02", 93.59],
                    "preferred_structure": "ABC zigzag / running terminal variation",
                    "pivots": {"A": ["2025-11-21", 98.89], "B": ["2025-12-08", 121.37], "C": ["2026-02-02", 93.59]},
                    "note": "The GETTEX low occurred on 2 February, later than the 21 January endpoint on the primary NYSE feed, because EUR/USD movement and venue sessions alter the strict local price extreme.",
                    "status": "probable complete",
                },
                "Minor_3": {
                    "start": ["2026-02-02", 93.59], "status": "active",
                    "Minute_i": {"end": ["2026-05-11", 225.80], "status": "probable complete five-wave impulse"},
                    "Minute_ii": {
                        "end": ["2026-05-19", 196.66],
                        "pivots": {"A": ["2026-05-12", 194.76], "B": ["2026-05-14", 220.00], "C": ["2026-05-19", 196.66]},
                        "status": "probable running Flat; C held slightly above A on GETTEX",
                    },
                    "Minute_iii": {
                        "start": ["2026-05-19", 196.66], "end": ["2026-06-02", 415.25],
                        "minuette_pivots": [196.66, 268.15, 256.50, 388.15, 345.55, 415.25],
                        "price_rules": impulse_rules(minute3_points),
                        "metrics": {
                            "i": metrics(two_hour, dt("2026-05-19T13:00:00"), dt("2026-05-26T11:00:00")),
                            "ii": metrics(two_hour, dt("2026-05-26T11:00:00"), dt("2026-05-27T13:00:00")),
                            "iii": metrics(two_hour, dt("2026-05-27T13:00:00"), dt("2026-05-29T05:30:00")),
                            "iv": metrics(two_hour, dt("2026-05-29T05:30:00"), dt("2026-05-29T15:00:00")),
                            "v": metrics(two_hour, dt("2026-05-29T15:00:00"), dt("2026-06-02T07:00:00")),
                        },
                        "status": "probable complete extended five-wave impulse",
                    },
                    "Minute_iv": {
                        "status": "active / not confirmed complete",
                        "leg_A_or_W": ["2026-06-09", 309.75],
                        "leg_B_or_X": ["2026-07-09", 402.35],
                        "B_retracement_pct": round(minute4_b_retrace, 2),
                        "strict_database_label": "Complex W-X-Y preferred because B/X is below the 90% Flat threshold",
                        "near_threshold_alternate": "Near-regular Flat; the NYSE feed exceeded 90%, and currency conversion can move a borderline ratio across the threshold",
                        "current_lower_degree": "The drop from 402.35 to 367.55 and rebound to 396.50 are too small and incomplete to prove that C/Y has begun.",
                        "metrics": {
                            "A_or_W": metrics(two_hour, dt("2026-06-02T07:00:00"), dt("2026-06-09T17:00:00")),
                            "B_or_X": metrics(two_hour, dt("2026-06-09T17:00:00"), dt("2026-07-09T17:00:00")),
                            "post_B_candidate": metrics(two_hour, dt("2026-07-09T17:00:00"), dt("2026-07-14T23:59:59")),
                        },
                        "targets_if_C_or_Y_decline_confirms": {
                            "0.618_of_A_from_B": round(402.35 - minute4_a_length * 0.618, 2),
                            "1.000_of_A_from_B": round(402.35 - minute4_a_length, 2),
                            "1.618_of_A_from_B": round(402.35 - minute4_a_length * 1.618, 2),
                        },
                        "hard_no_overlap_level": 225.80,
                    },
                    "Minute_v": "future after Minute iv completes",
                },
                "Minor_4": "future",
                "Minor_5": "future",
            },
            "Intermediate_4": "future",
            "Intermediate_5": "future",
        },
    },
    "decision_levels": {
        "415.25": "Minute iii high; a break can mean B/X extension or early Minute v, but requires a recount of the active correction",
        "402.35": "current B/X pivot candidate",
        "367.55": "first lower-degree support after the B/X high",
        "337.15": "0.618 A projection from 402.35",
        "309.75": "A/W low; a Flat C often tests or breaks it",
        "296.85": "A=C projection from 402.35",
        "225.80": "hard standard-impulse overlap level for Minute iv versus Minute i",
    },
    "final_assessment": {
        "preferred_count": "Cycle I > Primary 3 > Intermediate (3) > Minor 3 > Minute iv active",
        "important_revision": "The 415.25 GETTEX high is Minute iii, not completed Minor 3. Minute v, Minor 4, Minor 5, Intermediate (4), and Intermediate (5) remain ahead if the impulse count stays valid.",
        "confidence": "probable",
    },
}

OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report["final_assessment"], indent=2))
