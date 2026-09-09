import contextlib
import io
import json
import math
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


def enrich_relative_metrics(rows):
    for index, row in enumerate(rows):
        history = rows[max(0, index - 51):index + 1]
        average_volume = sum(item["volume"] for item in history) / len(history)
        row["relative_volume_52w"] = row["volume"] / average_volume if average_volume else 0
        median = (row["high"] + row["low"]) / 2
        row["ewo_pct"] = 100 * row["ewo"] / median if row["ewo"] is not None and median else None


def range_stats(rows, start, end):
    selected = [row for row in rows if dt(start) <= row["timestamp"] <= dt(end)]
    return {
        "bars": len(selected),
        "average_relative_volume_52w": round(sum(row["relative_volume_52w"] for row in selected) / len(selected), 3),
        "maximum_rsi": round(max(row["rsi"] for row in selected if row["rsi"] is not None), 2),
        "ending_rsi": round(selected[-1]["rsi"], 2),
        "maximum_directional_ewo_pct": round(max(row["ewo_pct"] for row in selected if row["ewo_pct"] is not None), 2),
        "cumulative_volume": round(sum(row["volume"] for row in selected)),
        "average_volume": round(sum(row["volume"] for row in selected) / len(selected)),
    }


def pivot_stats(rows, date):
    pivot = min(rows, key=lambda row: abs((row["timestamp"] - dt(date)).total_seconds()))
    return {
        "date": pivot["timestamp"].date().isoformat(),
        "high": pivot["high"],
        "low": pivot["low"],
        "close": pivot["close"],
        "rsi": round(pivot["rsi"], 2),
        "ewo": round(pivot["ewo"], 4),
        "ewo_pct": round(pivot["ewo_pct"], 2),
        "volume": round(pivot["volume"]),
        "volume_vs_52w": round(pivot["relative_volume_52w"], 2),
    }


def log_motive_lengths(points):
    return [
        math.log(points[1] / points[0]),
        math.log(points[3] / points[2]),
        math.log(points[5] / points[4]),
    ]


enrich_relative_metrics(weekly)

preferred_points = [0.0447, 4.25, 0.7544, 69.10, 16.43, 134.12]
alternate_points = [0.0447, 0.5657, 0.1273, 4.25, 0.7544, 69.10]

primary_1_internal = [0.0447, 0.5657, 0.1273, 1.83, 1.36, 4.25]
primary_1_intermediate_3 = [0.1273, 0.4163, 0.2907, 0.7177, 0.5293, 1.83]
primary_3_internal = [0.7544, 6.54, 4.57, 42.32, 24.13, 69.10]
primary_3_intermediate_3 = [4.57, 6.87, 5.87, 33.14, 27.12, 42.32]
primary_3_intermediate_5 = [24.13, 38.60, 25.23, 59.33, 48.21, 69.10]
primary_5_internal = [16.43, 45.65, 35.21, 106.45, 85.39, 134.12]

preferred_logs = log_motive_lengths(preferred_points)
alternate_logs = log_motive_lengths(alternate_points)

preferred_wave_stats = {
    "Primary_1": range_stats(weekly, "2002-09-30", "2011-07-10T23:59:59"),
    "Primary_3": range_stats(weekly, "2012-07-30", "2021-10-31T23:59:59"),
    "Primary_5": range_stats(weekly, "2022-06-13", "2025-06-30T23:59:59"),
}

primary_5_metrics = {
    "1": metrics(weekly, dt("2022-06-13"), dt("2023-07-16T23:59:59")),
    "2": metrics(weekly, dt("2023-07-10"), dt("2023-10-15T23:59:59")),
    "3": metrics(weekly, dt("2023-10-09"), dt("2025-02-16T23:59:59")),
    "4": metrics(weekly, dt("2025-02-10"), dt("2025-04-06T23:59:59")),
    "5": metrics(weekly, dt("2025-03-31"), dt("2025-07-06T23:59:59")),
}

primary_a = [134.12, 114.47, 126.57, 103.81, 109.73, 75.23]
primary_a_metrics = {
    "1": metrics(daily, dt("2025-06-30"), dt("2025-08-05T23:59:59")),
    "2": metrics(daily, dt("2025-08-05"), dt("2025-09-09T23:59:59")),
    "3": metrics(daily, dt("2025-09-09"), dt("2025-11-21T23:59:59")),
    "4": metrics(daily, dt("2025-11-21"), dt("2025-12-02T23:59:59")),
    "5": metrics(daily, dt("2025-12-02"), dt("2026-02-12T23:59:59")),
}

primary_c = [108.95, 91.30, 94.22, 70.86, 78.44, 72.28]
primary_c_metrics = {
    "1": metrics(daily, dt("2026-04-16"), dt("2026-04-27T23:59:59")),
    "2": metrics(daily, dt("2026-04-27"), dt("2026-04-30T23:59:59")),
    "3": metrics(daily, dt("2026-04-30"), dt("2026-06-25T23:59:59")),
    "4": metrics(daily, dt("2026-06-25"), dt("2026-07-02T23:59:59")),
    "5": metrics(daily, dt("2026-07-02"), dt("2026-07-15T23:59:59")),
}

primary_a_length = 134.12 - 75.23
cycle_length = 134.12 - 0.0447

report = {
    "symbol": "NASDAQ:NFLX",
    "audit_date": "2026-07-15",
    "method": "High-degree re-audit on linear and logarithmic scales using 1,258 weekly bars, RSI, price-normalized EWO, 52-week-relative volume, lower-degree subdivisions, and macro-rate context",
    "data_provenance": {
        "source": "Live TradingView Table View, split-adjusted NASDAQ:NFLX",
        "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
        "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
        "two_hour": {"rows": len(two_hour), "start": two_hour[0]["timestamp"].isoformat(), "end": two_hour[-1]["timestamp"].isoformat()},
    },
    "candidate_comparison": {
        "preferred_2025_cycle_top": {
            "points": preferred_points,
            "hard_rules": rules(preferred_points, "up"),
            "log_motive_lengths_1_3_5": [round(value, 3) for value in preferred_logs],
            "wave_3_to_wave_1_log_ratio": round(preferred_logs[1] / preferred_logs[0], 3),
            "wave_5_to_wave_3_log_ratio": round(preferred_logs[2] / preferred_logs[1], 3),
            "interpretation": "Primary 1 and 3 are nearly equal on log scale; Primary 5 is shorter, not extended.",
        },
        "alternate_2021_cycle_top": {
            "points": alternate_points,
            "hard_rules": rules(alternate_points, "up"),
            "log_motive_lengths_1_3_5": [round(value, 3) for value in alternate_logs],
            "wave_5_to_wave_3_log_ratio": round(alternate_logs[2] / alternate_logs[1], 3),
            "interpretation": "Valid by hard price rules, but Primary 5 is progressively extended on log scale and therefore less parsimonious.",
        },
        "decision": "Prefer the 2025 Cycle I top. It has near-equal Primary 1 and 3, a shorter terminal Primary 5, valid internals, and multi-indicator terminal divergence.",
    },
    "preferred_count": {
        "Cycle_I_complete": {
            "span": ["2002-09", 0.0447, "2025-06", 134.12],
            "Primary_waves": {"1": [0.0447, 4.25], "2": [4.25, 0.7544], "3": [0.7544, 69.10], "4": [69.10, 16.43], "5": [16.43, 134.12]},
            "rules": rules(preferred_points, "up"),
            "log_motive_lengths": [round(value, 3) for value in preferred_logs],
            "Primary_1": {
                "Intermediate_waves": primary_1_internal,
                "rules": rules(primary_1_internal, "up"),
                "Intermediate_3_subdivision": primary_1_intermediate_3,
                "Intermediate_3_rules": rules(primary_1_intermediate_3, "up"),
            },
            "Primary_2": {
                "span": [4.25, 0.7544],
                "retracement_pct": round((4.25 - 0.7544) / (4.25 - 0.0447) * 100, 2),
                "preferred_family": "Deep W-X-Y/complex correction",
                "structure": {"W": [4.25, 0.8986], "X": [0.8986, 1.85], "Y": [1.85, 0.7544]},
            },
            "Primary_3": {
                "Intermediate_waves": primary_3_internal,
                "rules": rules(primary_3_internal, "up"),
                "Intermediate_3_subdivision": primary_3_intermediate_3,
                "Intermediate_3_rules": rules(primary_3_intermediate_3, "up"),
                "Intermediate_5_subdivision": primary_3_intermediate_5,
                "Intermediate_5_rules": rules(primary_3_intermediate_5, "up"),
            },
            "Primary_4": {
                "span": [69.10, 16.43],
                "retracement_pct": round((69.10 - 16.43) / (69.10 - 0.7544) * 100, 2),
                "preferred_family": "W-X-Y/complex correction",
            },
            "Primary_5": {
                "Intermediate_waves": primary_5_internal,
                "rules": rules(primary_5_internal, "up"),
                "metrics": primary_5_metrics,
                "verification": verify(primary_5_internal, primary_5_metrics, "up"),
                "directional_assessment": directional(primary_5_metrics, "up"),
            },
        },
        "terminal_confirmation": {
            "wave_statistics": preferred_wave_stats,
            "pivot_statistics": {
                "Primary_1_top_2011": pivot_stats(weekly, "2011-07-05"),
                "Primary_3_momentum_peak_2018": pivot_stats(weekly, "2018-06-18"),
                "Primary_3_top_2021": pivot_stats(weekly, "2021-10-25"),
                "Primary_5_top_2025": pivot_stats(weekly, "2025-06-23"),
                "current_2026": pivot_stats(weekly, "2026-07-13"),
            },
            "RSI_divergence": "Primary 5 maximum weekly RSI 81.66 < Primary 3 maximum 86.39; top-week RSI 70.91 < 79.39.",
            "EWO_divergence": "Price-normalized EWO peak in Primary 5 was 25.21% versus 48.45% in Primary 3.",
            "volume_result": "Average 52-week-relative volume was 0.896 in Primary 5 versus 0.890 in Primary 3, so normalized volume does not provide strict divergence. Cumulative and raw average volume are lower, but split adjustment and duration make those comparisons secondary.",
        },
        "Cycle_II_active": {
            "Primary_A": {
                "span": [134.12, 75.23],
                "Intermediate_waves": primary_a,
                "rules": rules(primary_a, "down"),
                "verification": verify(primary_a, primary_a_metrics, "down"),
                "directional_assessment": directional(primary_a_metrics, "down"),
            },
            "Primary_B": {
                "span": [75.23, 108.95],
                "structure": [75.23, 100.19, 90.69, 108.95],
                "classification": "ABC zigzag",
                "B_retracement_of_A_pct": round((100.19 - 90.69) / (100.19 - 75.23) * 100, 2),
                "C_to_A_ratio": round((108.95 - 90.69) / (100.19 - 75.23), 3),
            },
            "Primary_C_active": {
                "Intermediate_waves": {"1": [108.95, 91.30], "2": [91.30, 94.22], "3": [94.22, 70.86], "4": [70.86, 78.44], "5": [78.44, 72.28, "active/possibly attempting a low"]},
                "rules_so_far": rules(primary_c, "down"),
                "verification_so_far": verify(primary_c, primary_c_metrics, "down"),
                "directional_assessment_so_far": directional(primary_c_metrics, "down"),
                "Intermediate_5_Minute_waves": {"i": [78.44, 75.72], "ii": [75.72, 78.18], "iii": [78.18, 72.52], "iv": [72.52, 75.39], "v": [75.39, 72.28, "active/unconfirmed"]},
                "minute_rules_so_far": rules([78.44, 75.72, 78.18, 72.52, 75.39, 72.28], "down"),
            },
        },
    },
    "macro_rate_context": {
        "role": "Treasury yields are contextual confirmation, not an Elliott labeling rule.",
        "observations": [
            "The 10-year Treasury monthly average was 3.00% in July 2011 and fell to about 2.17% by 10 August 2011 during the Primary 1 reversal regime.",
            "The effective federal funds rate was 0.08% in October 2021, then rose to a 1.21% monthly average by June 2022 as Primary 4 unfolded.",
            "The 10-year Treasury was 4.38% on 18 June 2025 near the Primary 5 top.",
            "The 10-year Treasury monthly average was 4.47% in June 2026 and 4.56% on 8 July 2026, maintaining a materially higher discount-rate regime during Cycle II.",
        ],
        "sources": [
            "https://fred.stlouisfed.org/series/DGS10",
            "https://fred.stlouisfed.org/series/FEDFUNDS",
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/",
        ],
        "inference": "The shift from near-zero short rates in 2021 to materially higher policy and long-term rates supports the interpretation of 2025 as a mature terminal advance, but does not prove the wave degree by itself.",
    },
    "targets": {
        "Primary_C_0_618_of_A": round(108.95 - 0.618 * primary_a_length, 2),
        "Primary_C_0_786_of_A": round(108.95 - 0.786 * primary_a_length, 2),
        "Primary_C_equality_A": round(108.95 - primary_a_length, 2),
        "Cycle_I_50_retracement": round(134.12 - 0.5 * cycle_length, 2),
        "Cycle_I_61_8_retracement": round(134.12 - 0.618 * cycle_length, 2),
        "Intermediate_5_0_618_of_wave_1": round(78.44 - 0.618 * (108.95 - 91.30), 2),
        "Intermediate_5_equality_wave_1": round(78.44 - (108.95 - 91.30), 2),
    },
    "current_verdict": {
        "preferred_state": "Cycle II > Primary C > Intermediate 5 > Minute v active or attempting a low",
        "first_completion_zone_reached": "$70.86-$72.56",
        "next_confluence": "$67.08-$67.53",
        "secondary_extension": "$60.79-$62.66",
        "deep_confluence": "$50.06-$51.26",
        "near_term_invalidation": "Above $78.44 invalidates Intermediate 4 as complete",
        "primary_c_invalidation": "Above $108.95 invalidates active Primary C",
        "cycle_ii_invalidation": "Above $134.12 invalidates Cycle II as still active",
        "bottom_confirmation": "Five up followed by a three-wave pullback that holds the terminal low; initial evidence above $78.44, stronger above $94.22, structural confirmation above $108.95.",
        "confidence": "Moderate-high on Cycle I ending in 2025; high on hard price rules and audited internals; strong RSI/normalized-EWO terminal evidence; neutral normalized-volume evidence; low on calling the current final low before reversal confirmation.",
    },
    "alternate": {
        "name": "Cycle I ended at $69.10 in 2021; Cycle III began at $16.43",
        "status": "Price-valid alternate, downgraded",
        "reason": "It passes hard rules but requires a progressively extended Primary 5 on log scale and loses the near-equality between the 2002-2011 and 2012-2021 motive waves.",
    },
}

serialized = json.dumps(report, indent=2)
(ROOT / "nflx_high_degree_reaudit_2026-07-15.json").write_text(serialized, encoding="utf-8")
(ROOT / "nflx_elliott_wave_analysis.json").write_text(serialized, encoding="utf-8")

md = """# NFLX High-Degree Re-Audit - 15 July 2026

## Verdict

The preferred count is restored to **Cycle I ending at $134.12 in June 2025**, but for a stronger reason than the original report: the full 1,258-week history has now been tested on logarithmic scale and its Primary 1, 3, and 5 internals have been audited.

The motive-wave log distances are:

- Primary 1: **4.555**
- Primary 3: **4.517**
- Primary 5: **2.100**

Primary 1 and 3 are nearly equal. Primary 5 is shorter, not extended. The 2021-ending alternate instead produces motive distances 2.538, 3.508, and 4.517, requiring a progressively extended Primary 5.

## Preferred Cycle I

- Primary 1: $0.0447 -> $4.25
- Primary 2: $4.25 -> $0.7544
- Primary 3: $0.7544 -> $69.10
- Primary 4: $69.10 -> $16.43
- Primary 5: $16.43 -> $134.12

Every motive wave and the audited lower-degree subdivisions pass Wave 3 length and Wave 4 no-overlap rules.

## Momentum And Volume

- Primary 3 maximum weekly RSI: **86.39**; Primary 5: **81.66**.
- RSI at the 2021 top: **79.39**; at the 2025 top: **70.91**.
- Price-normalized EWO peak: **48.45%** in Primary 3 versus **25.21%** in Primary 5.
- Average 52-week-relative volume: **0.890** in Primary 3 versus **0.896** in Primary 5.

RSI and normalized EWO confirm terminal divergence. Properly normalized volume is neutral, so volume is not used to force the conclusion.

## Yield Context

The 10-year Treasury was about 4.38% near the June 2025 top and remained around a 4.47% monthly average in June 2026. This materially higher discount-rate regime supports a mature-cycle interpretation, but yields are context rather than an Elliott rule.

## Current Count

The preferred live hierarchy is **Cycle II > Primary C > Intermediate 5 > Minute v active or attempting a low**.

- First completion area already reached: **$70.86-$72.56**
- Next confluence: **$67.08-$67.53**
- Secondary extension: **$60.79-$62.66**
- Deep confluence: **$50.06-$51.26**

Above $78.44 invalidates the current Intermediate 5 placement; above $108.95 invalidates Primary C; above $134.12 invalidates Cycle II as still active. The bottom remains unconfirmed until five up is followed by a three-wave pullback that holds the low.
"""
(ROOT / "NFLX_High_Degree_Reaudit_Report_2026-07-15.md").write_text(md, encoding="utf-8")
print(serialized)
