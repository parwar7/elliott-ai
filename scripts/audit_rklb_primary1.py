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


def extrema(rows, start, end, window):
    selected = [row for row in rows if start <= row["timestamp"] <= end]
    found = []
    for index in range(window, len(selected) - window):
        block = selected[index - window:index + window + 1]
        row = selected[index]
        if row["high"] == max(item["high"] for item in block):
            found.append({"type": "H", "date": row["timestamp"].date().isoformat(), "price": row["high"]})
        if row["low"] == min(item["low"] for item in block):
            found.append({"type": "L", "date": row["timestamp"].date().isoformat(), "price": row["low"]})
    return found


def impulse_rules(points):
    lengths = [abs(points[1] - points[0]), abs(points[3] - points[2]), abs(points[5] - points[4])]
    return {
        "wave_lengths": [round(value, 2) for value in lengths],
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": points[4] > points[1],
        "wave_4_overlap_buffer": round(points[4] - points[1], 2),
    }


def wave_metrics(rows, definitions):
    return {
        name: base.metrics(rows, dt(start), dt(end))
        for name, start, end in definitions
    }


weekly = base.load(ROOT / "tv_rklb_weekly.json")
daily = base.load(ROOT / "tv_rklb_daily.json")

# Count A: the earlier report's completed Primary 1 interpretation.
count_a_points = [3.47, 33.34, 14.71, 99.58, 56.13, 151.00]
count_a_metrics = wave_metrics(weekly, [
    ("I1", "2024-04-15", "2025-01-26T23:59:59"),
    ("I2", "2025-01-20", "2025-04-13T23:59:59"),
    ("I3", "2025-04-07", "2026-01-18T23:59:59"),
    ("I4", "2026-01-12", "2026-04-05T23:59:59"),
    ("I5", "2026-03-30", "2026-05-31T23:59:59"),
])

# Count B: Primary 1 still active; $14.71-$151 is one extended Intermediate (3).
int3_points = [14.71, 53.44, 38.26, 99.58, 56.13, 151.00]
int3_metrics = wave_metrics(weekly, [
    ("Minor1", "2025-04-07", "2025-07-20T23:59:59"),
    ("Minor2", "2025-07-14", "2025-08-24T23:59:59"),
    ("Minor3", "2025-08-18", "2026-01-18T23:59:59"),
    ("Minor4", "2026-01-12", "2026-04-05T23:59:59"),
    ("Minor5", "2026-03-30", "2026-05-31T23:59:59"),
])

# Intermediate (1) remains the same under both counts.
int1_points = [3.47, 5.84, 4.20, 28.05, 21.87, 33.34]
int1_metrics = wave_metrics(weekly, [
    ("Minor1", "2024-04-15", "2024-07-21T23:59:59"),
    ("Minor2", "2024-07-15", "2024-08-11T23:59:59"),
    ("Minor3", "2024-08-05", "2024-12-01T23:59:59"),
    ("Minor4", "2024-11-25", "2024-12-22T23:59:59"),
    ("Minor5", "2024-12-16", "2025-01-26T23:59:59"),
])

report = {
    "symbol": "NASDAQ:RKLB",
    "audit_date": "2026-07-15",
    "scope": "Detailed re-audit of the April 2024 bull sequence previously labeled Primary 1",
    "weekly_extrema": extrema(weekly, dt("2024-04-01"), dt("2026-06-08"), 2),
    "daily_extrema_recent": extrema(daily, dt("2024-12-10"), dt("2026-06-08"), 5),
    "intermediate_1": {
        "points": int1_points,
        "rules": impulse_rules(int1_points),
        "metrics": int1_metrics,
        "verdict": "Valid five-wave candidate",
    },
    "count_A_primary_1_complete": {
        "points": count_a_points,
        "rules": impulse_rules(count_a_points),
        "metrics": count_a_metrics,
        "strengths": ["Parent impulse passes the two hard price rules", "Current decline is deep enough to be Primary 2"],
        "weaknesses": [
            "The proposed Intermediate (3) from $14.71 to $99.58 has no clean standard five-wave subdivision",
            "The supposed Intermediate (5) has the strongest weekly EWO and highest average volume, so no terminal divergence exists",
            "A seven-week Primary 2 would be unusually brief relative to the two-year Primary 1 advance",
        ],
    },
    "count_B_primary_1_active": {
        "known_intermediate": [["(1)", 3.47, 33.34], ["(2)", 33.34, 14.71], ["(3)", 14.71, 151.00], ["(4)", 151.00, 75.45]],
        "intermediate_3_points": int3_points,
        "intermediate_3_rules": impulse_rules(int3_points),
        "intermediate_3_metrics": int3_metrics,
        "strengths": [
            "$14.71-$151 subdivides into a clean five-wave sequence",
            "Minor 4 remains above Minor 1 by $2.69, avoiding overlap",
            "The current $151-$75.45 decline has a duration and depth natural for Intermediate (4)",
        ],
        "weaknesses": [
            "Minor 5 has stronger EWO than Minor 3, so the fifth-wave extension lacks classic momentum divergence",
            "Intermediate (4) has not yet confirmed complete and could extend",
        ],
    },
    "revised_verdict": {
        "preferred": "Primary 1 remains active; Intermediate (3) completed at $151 and Intermediate (4) is active or forming a low near $75.45-$75.60",
        "confidence": "moderate-high",
        "former_count": "Primary 1 complete at $151 and Primary 2 active",
        "former_count_status": "valid alternate, downgraded",
        "confirmation_needed": "A five-wave advance from the correction low followed by a three-wave pullback above that low; a later break above $151 would confirm Intermediate (5)",
        "invalidation": "$53.44 is support, not invalidation; a higher-degree Intermediate (4) may enter the internal Minor 1 territory of Intermediate (3). A sustained decline below $33.34 overlaps Primary 1 Intermediate (1) and invalidates the standard Primary 1 impulse structure.",
    },
}

(ROOT / "rklb_primary1_detailed_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
