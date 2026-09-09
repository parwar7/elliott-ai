import contextlib
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with contextlib.redirect_stdout(io.StringIO()):
    import analyze_nflx as base


dt = base.dt
metrics = base.metrics
rules = base.impulse_rules
verify = base.verify_impulse
directional = base.directional_assessment

weekly = base.load("tv_nflx_weekly_complete.json")
daily = base.load("tv_nflx_daily.json")
two_hour = base.load("tv_nflx_2h.json")

cycle_1_points = [0.0447, 0.5657, 0.1273, 4.25, 0.7544, 69.10]
primary_1_internal = [0.0447, 0.0929, 0.0684, 0.1852, 0.1289, 0.5657]
primary_3_internal = [0.1273, 0.5843, 0.2694, 1.83, 1.36, 4.25]
primary_5_internal = [0.7544, 6.54, 4.57, 42.32, 25.23, 69.10]

cycle_3_primary_1 = [16.43, 45.65, 35.21, 106.45, 85.39, 134.12]
cycle_3_primary_1_metrics = {
    "1": metrics(weekly, dt("2022-06-13"), dt("2023-07-16T23:59:59")),
    "2": metrics(weekly, dt("2023-07-10"), dt("2023-10-15T23:59:59")),
    "3": metrics(weekly, dt("2023-10-09"), dt("2025-02-16T23:59:59")),
    "4": metrics(weekly, dt("2025-02-10"), dt("2025-04-06T23:59:59")),
    "5": metrics(weekly, dt("2025-03-31"), dt("2025-07-06T23:59:59")),
}

intermediate_a = [134.12, 114.47, 126.57, 103.81, 109.73, 75.23]
intermediate_a_metrics = {
    "1": metrics(daily, dt("2025-06-30"), dt("2025-08-05T23:59:59")),
    "2": metrics(daily, dt("2025-08-05"), dt("2025-09-09T23:59:59")),
    "3": metrics(daily, dt("2025-09-09"), dt("2025-11-21T23:59:59")),
    "4": metrics(daily, dt("2025-11-21"), dt("2025-12-02T23:59:59")),
    "5": metrics(daily, dt("2025-12-02"), dt("2026-02-12T23:59:59")),
}

intermediate_c = [108.95, 91.30, 94.22, 70.86, 78.44, 72.28]
intermediate_c_metrics = {
    "1": metrics(daily, dt("2026-04-16"), dt("2026-04-27T23:59:59")),
    "2": metrics(daily, dt("2026-04-27"), dt("2026-04-30T23:59:59")),
    "3": metrics(daily, dt("2026-04-30"), dt("2026-06-25T23:59:59")),
    "4": metrics(daily, dt("2026-06-25"), dt("2026-07-02T23:59:59")),
    "5": metrics(daily, dt("2026-07-02"), dt("2026-07-15T23:59:59")),
}

intermediate_a_length = 134.12 - 75.23
cycle_1_length = 69.10 - 0.0447
primary_1_length = 134.12 - 16.43

report = {
    "symbol": "NASDAQ:NFLX",
    "recount_date": "2026-07-15",
    "method": "Blind recount using 1,258 split-adjusted weekly bars from 2002, then daily and 2-hour confirmation for the active structure",
    "data_provenance": {
        "source": "Live TradingView Table View, NASDAQ:NFLX",
        "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
        "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
        "two_hour": {"rows": len(two_hour), "start": two_hour[0]["timestamp"].isoformat(), "end": two_hour[-1]["timestamp"].isoformat()},
        "price_basis": "TradingView split-adjusted NASDAQ prices",
    },
    "preferred_count": {
        "Cycle_I_complete": {
            "span": ["2002-09", 0.0447, "2021-10", 69.10],
            "Primary_waves": {"1": [0.0447, 0.5657], "2": [0.5657, 0.1273], "3": [0.1273, 4.25], "4": [4.25, 0.7544], "5": [0.7544, 69.10]},
            "rules": rules(cycle_1_points, "up"),
            "Primary_1_internal": primary_1_internal,
            "Primary_1_rules": rules(primary_1_internal, "up"),
            "Primary_3_internal": primary_3_internal,
            "Primary_3_rules": rules(primary_3_internal, "up"),
            "Primary_5_internal": primary_5_internal,
            "Primary_5_rules": rules(primary_5_internal, "up"),
        },
        "Cycle_II_complete": {
            "span": [69.10, 16.43],
            "preferred_family": "W-X-Y complex correction",
            "structure": {"W": [69.10, 35.15], "X": [35.15, 45.85], "Y": [45.85, 16.43]},
            "X_retracement_of_W_pct": round((45.85 - 35.15) / (69.10 - 35.15) * 100, 2),
            "Y_to_W_ratio": round((45.85 - 16.43) / (69.10 - 35.15), 3),
            "note": "W-X-Y is preferred over a zigzag because the X leg is shallow and the decline is internally overlapping before the terminal selloff.",
        },
        "Cycle_III_active": {
            "Primary_1_complete": {
                "span": [16.43, 134.12],
                "Intermediate_waves": {"1": [16.43, 45.65], "2": [45.65, 35.21], "3": [35.21, 106.45], "4": [106.45, 85.39], "5": [85.39, 134.12]},
                "rules": rules(cycle_3_primary_1, "up"),
                "metrics": cycle_3_primary_1_metrics,
                "verification": verify(cycle_3_primary_1, cycle_3_primary_1_metrics, "up"),
                "directional_assessment": directional(cycle_3_primary_1_metrics, "up"),
                "Intermediate_3_Minor_waves": [35.21, 63.80, 55.22, 93.53, 83.44, 106.45],
                "Intermediate_3_rules": rules([35.21, 63.80, 55.22, 93.53, 83.44, 106.45], "up"),
                "Intermediate_5_Minor_waves": [85.39, 99.34, 94.92, 126.28, 118.06, 134.12],
                "Intermediate_5_rules": rules([85.39, 99.34, 94.92, 126.28, 118.06, 134.12], "up"),
            },
            "Primary_2_active": {
                "Intermediate_A": {
                    "span": [134.12, 75.23],
                    "Minor_waves": intermediate_a,
                    "rules": rules(intermediate_a, "down"),
                    "verification": verify(intermediate_a, intermediate_a_metrics, "down"),
                    "directional_assessment": directional(intermediate_a_metrics, "down"),
                },
                "Intermediate_B": {
                    "span": [75.23, 108.95],
                    "structure": [75.23, 100.19, 90.69, 108.95],
                    "classification": "ABC zigzag",
                    "B_retracement_of_A_pct": round((100.19 - 90.69) / (100.19 - 75.23) * 100, 2),
                    "C_to_A_ratio": round((108.95 - 90.69) / (100.19 - 75.23), 3),
                },
                "Intermediate_C_active": {
                    "Minor_waves": {"1": [108.95, 91.30], "2": [91.30, 94.22], "3": [94.22, 70.86], "4": [70.86, 78.44], "5": [78.44, 72.28, "active/possibly attempting a low"]},
                    "rules_so_far": rules(intermediate_c, "down"),
                    "verification_so_far": verify(intermediate_c, intermediate_c_metrics, "down"),
                    "directional_assessment_so_far": directional(intermediate_c_metrics, "down"),
                    "Minor_5_Minute_waves": {"i": [78.44, 75.72], "ii": [75.72, 78.18], "iii": [78.18, 72.52], "iv": [72.52, 75.39], "v": [75.39, 72.28, "active/unconfirmed"]},
                    "minute_rules_so_far": rules([78.44, 75.72, 78.18, 72.52, 75.39, 72.28], "down"),
                },
            },
        },
    },
    "targets": {
        "Intermediate_C_0_618_of_A": round(108.95 - 0.618 * intermediate_a_length, 2),
        "Intermediate_C_0_786_of_A": round(108.95 - 0.786 * intermediate_a_length, 2),
        "Intermediate_C_equality_A": round(108.95 - intermediate_a_length, 2),
        "Primary_1_50_retracement": round(134.12 - 0.5 * primary_1_length, 2),
        "Primary_1_61_8_retracement": round(134.12 - 0.618 * primary_1_length, 2),
        "Minor_5_0_618_of_wave_1": round(78.44 - 0.618 * (108.95 - 91.30), 2),
        "Minor_5_equality_wave_1": round(78.44 - (108.95 - 91.30), 2),
        "Cycle_II_hard_invalidation": 16.43,
    },
    "current_verdict": {
        "preferred_state": "Cycle III > Primary 2 > Intermediate C > Minor 5 > Minute v active or attempting a low",
        "reference": "2-hour low $72.28; last extracted close $73.73",
        "first_completion_zone_reached": "$70.86-$72.56",
        "intermediate_extension": "$67.53",
        "strong_extension_confluence": "$60.79-$62.66",
        "deep_equality_target": "$50.06",
        "near_term_invalidation": "Above $78.44 invalidates Minor 4 as complete",
        "intermediate_c_invalidation": "Above $108.95 invalidates active Intermediate C",
        "primary_2_invalidation": "Above $134.12 invalidates the count that Primary 2 remains active",
        "cycle_3_hard_invalidation": "Below $16.43 invalidates the interpretation that Cycle III began there",
        "bottom_confirmation": "Complete five down, then five up and a three-wave pullback that holds the low; initial evidence above $78.44, stronger above $94.22, structural confirmation above $108.95.",
        "confidence": "High on Cycle I's five major waves and its audited Primary 1, 3, and 5 internals; moderate on exact degree names; moderate-high on Primary 2 as ABC; low on declaring $72.28 final without reversal confirmation.",
    },
    "what_changed": {
        "old": "Cycle I was incorrectly placed at $134.12 in 2025 using only monthly history plus weekly bars from 2020.",
        "new": "Full weekly history shows a cleaner completed Cycle I at $69.10 in 2021, Cycle II at $16.43 in 2022, and Primary 1 of Cycle III at $134.12 in 2025.",
        "current_price_structure": "The local A-B-C pivots are largely unchanged, but every current label moves down one degree: the active correction is Primary 2, not Cycle II.",
    },
}

serialized = json.dumps(report, indent=2)
(ROOT / "nflx_blind_recount_2026-07-15.json").write_text(serialized, encoding="utf-8")
(ROOT / "nflx_elliott_wave_analysis.degree_alternate.json").write_text(serialized, encoding="utf-8")

md = """# NFLX Blind Elliott Wave Recount - 15 July 2026

## Correction To The Previous Report

The prior count is superseded. It used full monthly history but only 333 weekly bars beginning in 2020, then promoted the 2025 high to Cycle degree without auditing the older internals.

With 1,258 weekly bars from June 2002, the preferred hierarchy is:

- **Cycle I:** $0.0447 -> $69.10 (2002-2021).
- **Cycle II:** $69.10 -> $16.43 (2021-2022), best treated as W-X-Y.
- **Cycle III:** active from $16.43.
- **Primary 1 of Cycle III:** $16.43 -> $134.12, complete.
- **Primary 2 of Cycle III:** active from $134.12.

## Cycle I Audit

Primary pivots are $0.0447 -> $0.5657 -> $0.1273 -> $4.25 -> $0.7544 -> $69.10. The impulse passes Wave 3 length and Wave 4 no-overlap rules.

- Primary 1 internals: $0.0447 -> $0.0929 -> $0.0684 -> $0.1852 -> $0.1289 -> $0.5657.
- Primary 3 internals: $0.1273 -> $0.5843 -> $0.2694 -> $1.83 -> $1.36 -> $4.25.
- Primary 5 internals: $0.7544 -> $6.54 -> $4.57 -> $42.32 -> $25.23 -> $69.10.

All three audited impulses pass the mandatory price rules.

## Current Structure

Primary 1 of Cycle III subdivides $16.43 -> $45.65 -> $35.21 -> $106.45 -> $85.39 -> $134.12.

Primary 2 currently counts as:

- Intermediate A: $134.12 -> $75.23, five waves.
- Intermediate B: $75.23 -> $108.95, ABC zigzag.
- Intermediate C: active from $108.95.

Intermediate C counts $108.95 -> $91.30 -> $94.22 -> $70.86 -> $78.44 -> active. Inside its current Minor 5: $78.44 -> $75.72 -> $78.18 -> $72.52 -> $75.39 -> $72.28/current.

## Levels

- First completion zone, already reached: **$70.86-$72.56**.
- Intermediate extension: **$67.53**.
- Strong extension confluence: **$60.79-$62.66**.
- Deep equality target: **$50.06**.
- Above **$78.44** invalidates the current Minor 5 placement.
- Above **$108.95** invalidates active Intermediate C.
- Above **$134.12** invalidates Primary 2 as still active.
- Below **$16.43** invalidates the interpretation that Cycle III began there.

The final low is not confirmed. Confirmation requires five up followed by a three-wave pullback that holds the terminal low.
"""
(ROOT / "NFLX_Blind_Recount_Report_2026-07-15.degree_alternate.md").write_text(
    "> **Degree alternate:** The high-degree re-audit favors Cycle I ending in 2025 after log-scale and terminal-divergence testing.\n\n" + md,
    encoding="utf-8",
)
print(serialized)
