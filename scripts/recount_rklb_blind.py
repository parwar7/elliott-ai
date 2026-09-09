import contextlib
import io
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with contextlib.redirect_stdout(io.StringIO()):
    import analyze_rklb as base


def dt(value):
    return datetime.fromisoformat(value)


def impulse_rules(points):
    lengths = [points[1] - points[0], points[3] - points[2], points[5] - points[4]]
    return {
        "wave_lengths": {"1": round(lengths[0], 2), "3": round(lengths[1], 2), "5": round(lengths[2], 2)},
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": points[4] > points[1],
        "overlap_buffer": round(points[4] - points[1], 2),
        "mandatory_pass": lengths[1] > min(lengths[0], lengths[2]) and points[4] > points[1],
    }


def metrics(rows, definitions):
    return {name: base.metrics(rows, dt(start), dt(end)) for name, start, end in definitions}


def verify_impulse(points, wave_metrics):
    rules = impulse_rules(points)
    w1, w2, w3, w4, w5 = (wave_metrics[str(index)] for index in range(1, 6))
    return {
        "hard_rules": rules,
        "wave_3_strength": w3["cumulative_volume"] > w1["cumulative_volume"] and w3["ewo_peak_abs"] > w1["ewo_peak_abs"],
        "correction_volume_dry_up": w2["cumulative_volume"] < w1["cumulative_volume"] and w4["cumulative_volume"] < w3["cumulative_volume"],
        "wave_4_ewo_zero_cross": w4["ewo_zero_cross"],
        "strict_wave_5_divergence": w5["cumulative_volume"] < w3["cumulative_volume"] and w5["ewo_peak_abs"] < w3["ewo_peak_abs"],
    }


weekly = base.load(ROOT / "tv_rklb_weekly.json")
daily = base.load(ROOT / "tv_rklb_daily.json")
two_hour = base.load(ROOT / "tv_rklb_2h.json")

first_impulse_points = [3.47, 33.34, 14.71, 73.97, 37.57, 99.58]
first_impulse_metrics = metrics(weekly, [
    ("1", "2024-04-15", "2025-01-26T23:59:59"),
    ("2", "2025-01-20", "2025-04-13T23:59:59"),
    ("3", "2025-04-07", "2025-10-19T23:59:59"),
    ("4", "2025-10-13", "2025-11-23T23:59:59"),
    ("5", "2025-11-17", "2026-01-18T23:59:59"),
])

correction_points = [99.58, 63.87, 78.67, 56.13]
correction = {
    "points": {"start": 99.58, "A": 63.87, "B": 78.67, "C": 56.13},
    "A_length": round(99.58 - 63.87, 2),
    "B_retracement_of_A_pct": round((78.67 - 63.87) / (99.58 - 63.87) * 100, 2),
    "C_length": round(78.67 - 56.13, 2),
    "C_to_A_ratio": round((78.67 - 56.13) / (99.58 - 63.87), 3),
    "total_retracement_pct": round((99.58 - 56.13) / (99.58 - 3.47) * 100, 2),
    "classification": "Clean zigzag candidate: B near 38.2%-50%, C near 0.618 of A",
}

second_impulse_regular_points = [56.13, 93.10, 73.99, 138.37, 115.23, 151.00]
second_impulse_extended_points = [56.14, 93.10, 73.99, 138.37, 115.23, 156.20]
second_impulse_metrics = metrics(two_hour, [
    ("1", "2026-03-30T18:00", "2026-04-22T12:00"),
    ("2", "2026-04-22T12:00", "2026-04-29T14:00"),
    ("3", "2026-04-29T14:00", "2026-05-18T12:00"),
    ("4", "2026-05-18T12:00", "2026-05-19T14:00"),
    ("5", "2026-05-19T14:00", "2026-05-27T08:00"),
])

current_correction = {
    "points": {"start": 151.00, "A": 80.00, "B": 107.60, "C_current": 75.45},
    "retracement_of_second_impulse_pct": round((151.00 - 75.45) / (151.00 - 56.13) * 100, 2),
    "B_retracement_of_A_pct": round((107.60 - 80.00) / (151.00 - 80.00) * 100, 2),
    "C_to_A_ratio_so_far": round((107.60 - 75.45) / (151.00 - 80.00), 3),
    "C_0_618_target": round(107.60 - 0.618 * (151.00 - 80.00), 2),
    "second_impulse_0_786_retracement": round(151.00 - 0.786 * (151.00 - 56.13), 2),
    "hard_wave_2_invalidation": 56.13,
    "status": "Five-down C candidate exists, but higher-degree reversal is unconfirmed",
}

candidate_a = {
    "name": "New preferred: Primary 1 to $99.58; Primary 2 to $56.13; Primary 3 active",
    "hierarchy": {
        "Primary_1": [3.47, 99.58],
        "Primary_2": [99.58, 56.13],
        "Primary_3_Intermediate_1": [56.13, 151.00],
        "Primary_3_Intermediate_2": [151.00, 75.45, "active/unconfirmed low"],
    },
    "first_impulse_rules": impulse_rules(first_impulse_points),
    "primary_2_correction": correction,
    "second_impulse_regular_rules": impulse_rules(second_impulse_regular_points),
    "second_impulse_extended_hours_rules": impulse_rules(second_impulse_extended_points),
    "strengths": [
        "Separates two complete five-wave advances with a clean ABC correction",
        "The $99.58-$56.13 correction has B=41.44% of A and C=0.631 of A",
        "Both five-wave advances pass Wave 3 length and Wave 4 no-overlap rules",
        "The current decline is naturally Intermediate (2) of Primary 3",
    ],
    "weaknesses": [
        "Primary 2 is short in duration relative to Primary 1",
        "Current Intermediate (2) is very deep and remains close to the $56.13 hard invalidation",
    ],
}

candidate_b_points = [14.71, 53.44, 38.26, 99.58, 56.13, 151.00]
candidate_b = {
    "name": "Previous preferred: Primary 1 active with extended Intermediate (3) to $151",
    "rules": impulse_rules(candidate_b_points),
    "strengths": ["The $14.71-$151 sequence passes the two hard price rules"],
    "weaknesses": [
        "It absorbs a complete five-wave advance to $99.58 and a near-textbook ABC to $56.13 into one larger impulse",
        "That degree assignment is less parsimonious than motive-correction-motive",
        "The prior targeted backtest preselected its pivots and therefore did not establish the degree labels",
    ],
    "status": "Valid alternate, downgraded",
}

candidate_c_points = [3.47, 33.34, 14.71, 99.58, 56.13, 151.00]
candidate_c = {
    "name": "Old count: Primary 1 complete at $151",
    "parent_rules": impulse_rules(candidate_c_points),
    "internal_problem": "The proposed $14.71-$99.58 Wave 3 does not have a clean standard five-wave subdivision without overlap.",
    "status": "Rejected as preferred; retained only as a low-confidence alternate",
}

report = {
    "symbol": "NASDAQ:RKLB",
    "recount_date": "2026-07-15",
    "method": "Blind recount from major confirmed swings, comparing motive-correction nesting before assigning degrees",
    "preferred_count": candidate_a,
    "completed_structures": {
        "first_impulse": {"points": first_impulse_points, "rules": impulse_rules(first_impulse_points), "metrics": first_impulse_metrics, "verification": verify_impulse(first_impulse_points, first_impulse_metrics)},
        "primary_2_zigzag": correction,
        "second_impulse": {
            "regular_session_points": second_impulse_regular_points,
            "regular_session_rules": impulse_rules(second_impulse_regular_points),
            "extended_hours_points": second_impulse_extended_points,
            "extended_hours_rules": impulse_rules(second_impulse_extended_points),
            "metrics": second_impulse_metrics,
            "verification": verify_impulse(second_impulse_extended_points, second_impulse_metrics),
        },
        "current_correction": current_correction,
    },
    "alternates": [candidate_b, candidate_c],
    "verdict": {
        "highest_confidence_completed_structure": "Five waves from $3.47 to $99.58, then an ABC zigzag to $56.13",
        "current_preferred_hierarchy": "Primary 3 > Intermediate (2) active or forming a low",
        "current_low": "$75.45 is only a lower-degree low candidate",
        "bullish_confirmation": "A five-wave advance from the final correction low, then a three-wave pullback that stays above it; stronger above $107.60 and confirmed above $151/$156.20",
        "hard_invalidation": "Below $56.13 invalidates the interpretation that Primary 3 began there",
        "degree_confidence": "Moderate. The swing structure is stronger than the exact Primary/Intermediate degree names.",
    },
}

serialized = json.dumps(report, indent=2)
(ROOT / "rklb_blind_recount_2026-07-15.json").write_text(serialized, encoding="utf-8")
(ROOT / "rklb_elliott_wave_analysis.json").write_text(serialized, encoding="utf-8")
print(json.dumps(report, indent=2))
