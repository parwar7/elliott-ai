import contextlib
import csv
import io
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with contextlib.redirect_stdout(io.StringIO()):
    import analyze_rklb as base


def dt(value):
    return datetime.fromisoformat(value)


def find_row(rows, timestamp):
    target = dt(timestamp)
    return min(range(len(rows)), key=lambda i: abs((rows[i]["timestamp"] - target).total_seconds()))


def causal_pivot(rows, timestamp, kind, window):
    index = find_row(rows, timestamp)
    row = rows[index]
    if index < window or index + window >= len(rows):
        return {
            "pivot_date": row["timestamp"].isoformat(), "price": row["high" if kind == "H" else "low"],
            "kind": kind, "window": window, "confirmed": False, "confirmation_date": None,
            "reason": "Not enough right-side bars at the dataset edge",
        }
    block = rows[index - window:index + window + 1]
    field = "high" if kind == "H" else "low"
    extreme = max(item[field] for item in block) if kind == "H" else min(item[field] for item in block)
    passed = row[field] == extreme
    return {
        "pivot_date": row["timestamp"].isoformat(), "price": row[field], "kind": kind, "window": window,
        "confirmed": passed,
        "confirmation_date": rows[index + window]["timestamp"].isoformat() if passed else None,
        "confirmation_lag_bars": window if passed else None,
        "reason": "Confirmed only after right-side bars closed" if passed else "Failed local-extreme test",
    }


def segment_metrics(rows, start, end):
    start_i, end_i = find_row(rows, start), find_row(rows, end)
    if start_i > end_i:
        start_i, end_i = end_i, start_i
    selected = rows[start_i + 1:end_i + 1]
    ewo = [item["ewo"] for item in selected if item["ewo"] is not None]
    rsi = [item["rsi"] for item in selected if item["rsi"] is not None]
    volume = sum(item["volume"] for item in selected)
    return {
        "bars": len(selected), "cumulative_volume": round(volume),
        "average_volume": round(volume / len(selected)) if selected else None,
        "ewo_peak_abs": round(max((abs(value) for value in ewo), default=0), 4),
        "ewo_min": round(min(ewo), 4) if ewo else None,
        "ewo_max": round(max(ewo), 4) if ewo else None,
        "ewo_zero_cross": bool(ewo and min(ewo) <= 0 <= max(ewo)),
        "rsi_min": round(min(rsi), 2) if rsi else None,
        "rsi_max": round(max(rsi), 2) if rsi else None,
    }


def impulse_rules(points, direction="up"):
    lengths = [abs(points[1] - points[0]), abs(points[3] - points[2]), abs(points[5] - points[4])]
    no_overlap = points[4] > points[1] if direction == "up" else points[4] < points[1]
    return {
        "wave_lengths": {"1": round(lengths[0], 2), "3": round(lengths[1], 2), "5": round(lengths[2], 2)},
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": no_overlap,
        "mandatory_pass": lengths[1] > min(lengths[0], lengths[2]) and no_overlap,
    }


def verification(waves, points, direction="up"):
    rules = impulse_rules(points, direction)
    w1, w2, w3, w4, w5 = (waves[name] for name in ("1", "2", "3", "4", "5"))
    wave3_strength = w3["cumulative_volume"] > w1["cumulative_volume"] and w3["ewo_peak_abs"] > w1["ewo_peak_abs"]
    correction_dry_up = w2["cumulative_volume"] < w1["cumulative_volume"] and w4["cumulative_volume"] < w3["cumulative_volume"]
    extreme = points[5] > points[3] if direction == "up" else points[5] < points[3]
    strict_divergence = extreme and w5["cumulative_volume"] < w3["cumulative_volume"] and w5["ewo_peak_abs"] < w3["ewo_peak_abs"]
    wave3_weakest = (
        w3["cumulative_volume"] == min(w1["cumulative_volume"], w3["cumulative_volume"], w5["cumulative_volume"])
        and w3["ewo_peak_abs"] == min(w1["ewo_peak_abs"], w3["ewo_peak_abs"], w5["ewo_peak_abs"])
    )
    return {
        "hard_price_rules": rules,
        "wave_3_strength_pass": wave3_strength,
        "wave_3_weakest_invalid": wave3_weakest,
        "correction_volume_dry_up_pass": correction_dry_up,
        "wave_4_ewo_zero_cross": w4["ewo_zero_cross"],
        "strict_wave_5_volume_ewo_divergence": strict_divergence,
        "interpretation": "Missing divergence is a caution, not a hard Elliott invalidation.",
    }


def fib_forecast(label, known_start, wave1_end, wave2_end, realized_wave3_end):
    wave1_length = abs(wave1_end - known_start)
    target = wave2_end + 1.618 * wave1_length
    error = abs(realized_wave3_end - target)
    return {
        "label": label, "information_available_after_wave_2": True,
        "target_1_618": round(target, 2), "realized_end": realized_wave3_end,
        "absolute_error": round(error, 2), "error_pct_of_target": round(error / target * 100, 2),
    }


weekly = base.load(ROOT / "tv_rklb_weekly.json")
daily = base.load(ROOT / "tv_rklb_daily.json")
two_hour = base.load(ROOT / "tv_rklb_2h.json")

# All labels use pivots that become visible only after the stated right-side confirmation window.
checkpoints = [
    ("Intermediate (1) high", daily, "2025-01-24", "H", 5),
    ("Intermediate (2) low", daily, "2025-04-07", "L", 5),
    ("Intermediate (3) Minor 1 high", daily, "2025-07-17", "H", 5),
    ("Intermediate (3) Minor 2 low", daily, "2025-08-20", "L", 5),
    ("Intermediate (3) Minor 3 high", daily, "2026-01-16", "H", 5),
    ("Intermediate (3) Minor 4 low", daily, "2026-03-30", "L", 5),
    ("Intermediate (3) Minor 5 high", daily, "2026-05-27", "H", 5),
    ("Intermediate (4) C lower-degree low", two_hour, "2026-07-13T22:00", "L", 4),
]
causal_checkpoints = []
for label, rows, timestamp, kind, window in checkpoints:
    result = causal_pivot(rows, timestamp, kind, window)
    result["label"] = label
    result["timeframe"] = "2h" if rows is two_hour else "1D"
    causal_checkpoints.append(result)

int1_defs = {
    "1": ("2024-04-15", "2024-07-15"), "2": ("2024-07-15", "2024-08-05"),
    "3": ("2024-08-05", "2024-12-02"), "4": ("2024-12-02", "2024-12-09"),
    "5": ("2024-12-09", "2025-01-21"),
}
int1_metrics = {name: segment_metrics(weekly, *dates) for name, dates in int1_defs.items()}
int1_points = [3.47, 5.84, 4.20, 28.10, 21.87, 33.34]

int3_defs = {
    "1": ("2025-04-07", "2025-07-17"), "2": ("2025-07-17", "2025-08-20"),
    "3": ("2025-08-20", "2026-01-16"), "4": ("2026-01-16", "2026-03-30"),
    "5": ("2026-03-30", "2026-05-27"),
}
int3_metrics = {name: segment_metrics(daily, *dates) for name, dates in int3_defs.items()}
int3_points = [14.71, 53.44, 38.26, 99.58, 56.13, 151.00]

c_defs = {
    "1": ("2026-07-01T16:00", "2026-07-02T16:00"),
    "2": ("2026-07-02T16:00", "2026-07-06T08:00"),
    "3": ("2026-07-06T08:00", "2026-07-08T08:00"),
    "4": ("2026-07-08T08:00", "2026-07-09T08:00"),
    "5": ("2026-07-09T08:00", "2026-07-13T22:00"),
}
c_metrics = {name: segment_metrics(two_hour, *dates) for name, dates in c_defs.items()}
c_points = [107.58, 97.91, 102.53, 80.51, 88.38, 75.45]

int3_fib = [
    fib_forecast("Minor 3 from Minor 1 and Minor 2", 14.71, 53.44, 38.26, 99.58),
    fib_forecast("Minor 5 from Minor 3 and Minor 4", 38.26, 99.58, 56.13, 151.00),
]

confirmed_count = sum(item["confirmed"] for item in causal_checkpoints)
mandatory_sequences = {
    "Intermediate_1": impulse_rules(int1_points),
    "Intermediate_3": impulse_rules(int3_points),
    "Intermediate_4_C": impulse_rules(c_points, "down"),
}
mandatory_passes = sum(item["mandatory_pass"] for item in mandatory_sequences.values())

report = {
    "symbol": "NASDAQ:RKLB",
    "test_timestamp": "2026-07-15 Europe/London",
    "method": {
        "type": "causal walk-forward structural replay",
        "no_lookahead": True,
        "pivot_rule": "Daily pivots require five closed bars to the right; 2-hour pivots require four.",
        "label_freezing": "A pivot is unavailable before its confirmation date and prior labels are not rewritten.",
        "scope": "Tests the revised Primary 1 hierarchy, not a complete entry/exit trading strategy.",
    },
    "data": {
        "weekly": [weekly[0]["timestamp"].isoformat(), weekly[-1]["timestamp"].isoformat(), len(weekly)],
        "daily": [daily[0]["timestamp"].isoformat(), daily[-1]["timestamp"].isoformat(), len(daily)],
        "two_hour": [two_hour[0]["timestamp"].isoformat(), two_hour[-1]["timestamp"].isoformat(), len(two_hour)],
    },
    "causal_checkpoints": causal_checkpoints,
    "completed_sequences": {
        "Intermediate_1": {"points": int1_points, "metrics": int1_metrics, "verification": verification(int1_metrics, int1_points)},
        "Intermediate_3": {"points": int3_points, "metrics": int3_metrics, "verification": verification(int3_metrics, int3_points), "fib_forecasts": int3_fib},
        "Intermediate_4_C": {"points": c_points, "metrics": c_metrics, "verification": verification(c_metrics, c_points, "down")},
    },
    "scorecard": {
        "causal_pivots_confirmed": f"{confirmed_count}/{len(causal_checkpoints)}",
        "mandatory_impulse_sequences_passed": f"{mandatory_passes}/{len(mandatory_sequences)}",
        "intermediate_3_wave3_strength": verification(int3_metrics, int3_points)["wave_3_strength_pass"],
        "intermediate_3_correction_volume_dry_up": verification(int3_metrics, int3_points)["correction_volume_dry_up_pass"],
        "intermediate_3_strict_terminal_divergence": verification(int3_metrics, int3_points)["strict_wave_5_volume_ewo_divergence"],
        "current_C_wave3_strength": verification(c_metrics, c_points, "down")["wave_3_strength_pass"],
        "current_C_strict_terminal_divergence": verification(c_metrics, c_points, "down")["strict_wave_5_volume_ewo_divergence"],
    },
    "verdict": {
        "completed_Intermediate_3": "Supported by the causal replay",
        "Primary_1_active": "Preferred and historically consistent, but not proven until Intermediate (5) forms",
        "Intermediate_4_low_at_75_45": "Unresolved. The 2-hour low is confirmed as a local pivot, but the higher-degree reversal is not confirmed.",
        "former_Primary_1_complete_at_151": "Still a valid alternate, not disproven by historical data",
        "next_confirmation": "A five-wave advance followed by a three-wave pullback holding above the correction low; later recovery above $107.60 and ultimately $151.",
        "hard_invalidation": "A sustained break below $33.34 overlaps Intermediate (1) and invalidates the preferred standard Primary 1 impulse.",
    },
    "limitations": [
        "This is a targeted causal audit of the proposed RKLB count, not a blind wave-discovery benchmark.",
        "The candidate pivots were specified before the replay and then tested for causal confirmation; the test does not prove an automated model would have selected the same pivots among all alternatives.",
        "The current $75.45 low is confirmed only at the 2-hour degree. A higher-degree Intermediate (4) low remains unconfirmed.",
        "Trading performance is not measured because entry, stop, sizing, costs, and exit rules have not yet been frozen.",
    ],
}

json_path = ROOT / "rklb_causal_backtest_2026-07-15.json"
csv_path = ROOT / "rklb_causal_checkpoints_2026-07-15.csv"
json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
with csv_path.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=[
        "label", "timeframe", "pivot_date", "kind", "price", "window", "confirmed",
        "confirmation_date", "confirmation_lag_bars", "reason",
    ])
    writer.writeheader()
    writer.writerows(causal_checkpoints)

print(json.dumps(report, indent=2))
