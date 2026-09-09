import json
import math
from pathlib import Path

import recount_mu_blind as tools


ROOT = Path(__file__).resolve().parents[1]


def ratio(a, b):
    return round(a / b, 4)


def retracement(start, end, correction, logarithmic=False):
    if logarithmic:
        return round(math.log(end / correction) / math.log(end / start) * 100, 2)
    return round((end - correction) / (end - start) * 100, 2)


def wave_metrics(rows, spans):
    return {label: tools.metrics(rows, start, end) for label, (start, end) in spans.items()}


def build_report():
    monthly = tools.normalize(tools.base.load(ROOT / "tv_mu_monthly.json"))
    weekly = tools.normalize(tools.base.load(ROOT / "tv_mu_weekly.json"))
    daily = tools.normalize(tools.base.load(ROOT / "tv_mu_daily.json"))
    four_hour = tools.normalize(tools.base.load(ROOT / "tv_mu_4h.json"))

    cycle_primary = [1.55, 11.65, 5.03, 1254.81]
    primary_3 = [5.03, 35.68, 9.08, 96.24, 47.52, 1254.81]
    intermediate_3 = [9.08, 63.05, 27.68, 94.55, 64.13, 96.24]
    intermediate_5 = [47.52, 156.41, 61.38, 455.24, 311.44, 1254.81]
    minor_3 = [61.38, 129.52, 103.21, 260.33, 192.40, 455.24]
    minor_5 = [311.44, 818.54, 652.11, 1089.12, 854.22, 1254.81]
    current_a = [1254.62, 1119.63, 1198.42, 875.01, 1035.50, 851.33]

    i5_metrics = wave_metrics(weekly, {
        "1": ("2022-09-19", "2024-06-17T23:59:59"),
        "2": ("2024-06-17", "2025-04-07T23:59:59"),
        "3": ("2025-04-07", "2026-01-30T23:59:59"),
        "4": ("2026-01-26", "2026-03-31T23:59:59"),
        "5": ("2026-03-30", "2026-06-25T23:59:59"),
    })
    minor_5_metrics = wave_metrics(daily, {
        "1": ("2026-03-31", "2026-05-11T23:59:59"),
        "2": ("2026-05-11", "2026-05-19T23:59:59"),
        "3": ("2026-05-19", "2026-06-03T23:59:59"),
        "4": ("2026-06-03", "2026-06-09T23:59:59"),
        "5": ("2026-06-09", "2026-06-25T23:59:59"),
    })
    current_a_metrics = wave_metrics(four_hour, {
        "1": ("2026-06-25T12:00", "2026-06-26T12:00"),
        "2": ("2026-06-26T12:00", "2026-06-26T16:00"),
        "3": ("2026-06-26T16:00", "2026-07-08T08:00"),
        "4": ("2026-07-08T08:00", "2026-07-09T12:00"),
        "5": ("2026-07-09T12:00", "2026-07-16T12:00"),
    })

    primary_3_log_lengths = [
        math.log(primary_3[1] / primary_3[0]),
        math.log(primary_3[3] / primary_3[2]),
        math.log(primary_3[5] / primary_3[4]),
    ]
    local_advance = minor_5[-1] - minor_5[0]
    primary_3_log_range = math.log(primary_3[-1] / primary_3[0])

    report = {
        "symbol": "NASDAQ:MU",
        "analysis_date": "2026-07-16",
        "scope": "November 2008 low through the current four-hour bar",
        "method": "Pattern-first, multi-scale recount; degree assigned after price rules, scale, Fibonacci, RSI/EWO, volume, duration, and channel checks",
        "data": {
            "monthly": {"rows": len(monthly), "start": monthly[0]["timestamp"].isoformat(), "end": monthly[-1]["timestamp"].isoformat()},
            "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
            "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
            "four_hour": {"rows": len(four_hour), "start": four_hour[0]["timestamp"].isoformat(), "end": four_hour[-1]["timestamp"].isoformat()},
            "live_four_hour_bar": four_hour[-1],
        },
        "scale_decision": {
            "2008_present": "Logarithmic is mandatory for wave proportion because price expanded more than 800-fold.",
            "2022_present": "Use logarithmic structure and arithmetic retracements together.",
            "daily_four_hour": "Use arithmetic Fibonacci levels, with percentage normalization for momentum comparisons.",
        },
        "preferred_high_degree_count": {
            "Cycle_I": {
                "status": "Active; Primary 3 may have completed at $1,254.81 and Primary 4 may be starting",
                "start": ["2008-11", 1.55],
                "Primary_1": {"path": [1.55, 11.65], "dates": ["2008-11", "2011-02"]},
                "Primary_2": {
                    "path": [11.65, 5.03],
                    "dates": ["2011-02", "2012-10"],
                    "linear_retracement_pct": retracement(1.55, 11.65, 5.03),
                    "log_retracement_pct": retracement(1.55, 11.65, 5.03, True),
                    "classification": "Complex correction candidate",
                },
                "Primary_3": {
                    "path": [5.03, 1254.81],
                    "dates": ["2012-10", "2026-06"],
                    "status": "Completion candidate",
                    "intermediate_points": primary_3,
                    "rules": tools.impulse_rules(primary_3),
                    "log_fibonacci": {
                        "wave_3_to_wave_1": ratio(primary_3_log_lengths[1], primary_3_log_lengths[0]),
                        "wave_5_to_wave_1": ratio(primary_3_log_lengths[2], primary_3_log_lengths[0]),
                        "wave_2_retracement_pct": retracement(5.03, 35.68, 9.08, True),
                        "wave_4_retracement_pct": retracement(9.08, 96.24, 47.52, True),
                    },
                    "alternation": "Intermediate 2 is deep on log scale; Intermediate 4 is shallow and shorter. This is healthy alternation.",
                    "channel": "The final advance throws over the long-term logarithmic 2-4 channel, supporting a climax but not proving the top without reversal confirmation.",
                },
                "Primary_4": {"status": "Active candidate from $1,254.81; degree remains unconfirmed"},
                "Primary_5": {"status": "Future if the preferred Cycle I count remains valid"},
            },
            "why_2022_is_not_preferred_as_Cycle_II": "The $96.24-$47.52 decline lasted only nine months and retraced roughly 30% of the preceding 2016-2022 advance on log scale. It is more proportionate as Intermediate 4 inside Primary 3 than as a complete Cycle-degree correction.",
        },
        "primary_3_detail": {
            "Intermediate_1": {"path": [5.03, 35.68], "dates": ["2012-10", "2014-12"]},
            "Intermediate_2": {"path": [35.68, 9.08], "dates": ["2014-12", "2016-01"], "log_retracement_pct": retracement(5.03, 35.68, 9.08, True)},
            "Intermediate_3": {
                "path": [9.08, 96.24],
                "dates": ["2016-01", "2022-01"],
                "minor_points": intermediate_3,
                "rules": tools.impulse_rules(intermediate_3),
            },
            "Intermediate_4": {"path": [96.24, 47.52], "dates": ["2022-01", "2022-09"], "log_retracement_pct": retracement(9.08, 96.24, 47.52, True)},
            "Intermediate_5": {
                "path": [47.52, 1254.81],
                "dates": ["2022-09", "2026-06"],
                "minor_points": intermediate_5,
                "rules": tools.impulse_rules(intermediate_5),
                "weekly_metrics": i5_metrics,
                "higher_timeframe_verdict": "The structure is complete, but weekly EWO and volume peak in Wave 5 while RSI only slightly diverges. This permits a top but does not provide strong high-timeframe exhaustion confirmation.",
            },
        },
        "lower_degree_detail": {
            "Intermediate_5_Minor_1": {"path": [47.52, 156.41], "classification": "Five-wave impulse"},
            "Intermediate_5_Minor_2": {"path": [156.41, 61.38], "classification": "Complex correction; exact W-X-Y versus W-X-Y-X-Z unresolved"},
            "Intermediate_5_Minor_3": {
                "path": [61.38, 455.24],
                "minute_points": minor_3,
                "rules": tools.impulse_rules(minor_3),
                "fibonacci": {"wave_3_to_wave_1": 2.3058, "wave_5_to_wave_3": 1.6729},
            },
            "Intermediate_5_Minor_4": {
                "path": [455.24, 311.44],
                "retracement_pct": retracement(61.38, 455.24, 311.44),
                "structure": "ABC zigzag candidate: $455.24 -> $363.69 -> $438.52 -> $311.44",
                "C_to_A_ratio": 1.3881,
            },
            "Intermediate_5_Minor_5": {
                "path": [311.44, 1254.81],
                "minute_points": minor_5,
                "rules": tools.impulse_rules(minor_5),
                "fibonacci": {"wave_2_retracement_pct": 32.82, "wave_3_to_wave_1": 0.8618, "wave_4_retracement_pct": 53.75, "wave_5_to_wave_1": 0.7900},
                "daily_metrics": minor_5_metrics,
                "verdict": "A normal impulse, not a diagonal: Wave 4 does not overlap Wave 1. RSI and EWO diverge in Wave 5; volume does not dry up.",
            },
        },
        "current_four_hour": {
            "classification": "Five-down first leg from $1,254.62; Wave A candidate within a larger correction",
            "points": current_a,
            "rules": tools.impulse_rules(current_a),
            "metrics": current_a_metrics,
            "live_status": "Wave v reached $851.33 on the active July 16 four-hour bar. No reversal is confirmed.",
            "divergence": "Wave v undercuts Wave iii while RSI, EWO, and average relative volume are less bearish than in Wave iii. This supports an approaching A-leg low, not a completed correction.",
            "local_retracement_of_311_1254_pct": round((1254.81 - 851.33) / local_advance * 100, 2),
            "local_support": {"38.2_pct": 894.44, "50_pct": 783.13, "61.8_pct": 671.81},
            "conditional_B_zone_if_851_33_holds": {"38.2_pct": 1005.39, "50_pct": 1052.98, "61.8_pct": 1100.56},
            "confirmation": "Require five waves up, followed by three waves down that hold above the final low.",
        },
        "primary_4_log_targets_if_1254_81_is_primary_3": {
            "23.6_pct": round(math.exp(math.log(1254.81) - 0.236 * primary_3_log_range), 2),
            "38.2_pct": round(math.exp(math.log(1254.81) - 0.382 * primary_3_log_range), 2),
            "50_pct": round(math.exp(math.log(1254.81) - 0.5 * primary_3_log_range), 2),
            "warning": "These are high-degree context levels, not immediate forecasts. A Primary correction would normally take much longer than the current decline.",
        },
        "alternates": [
            {
                "name": "Cycle I ended at $96.24; Cycle II ended at $47.52; Cycle III active",
                "confidence": "Moderate alternate",
                "weakness": "The proposed Cycle II is unusually brief and shallow on logarithmic scale relative to the 2008-2022 advance.",
            },
            {
                "name": "$1,254.81 ended only a lower-degree third; another fifth remains",
                "confidence": "Serious bullish alternate",
                "strength": "Weekly EWO and volume made new highs into $1,254.81, so higher-timeframe momentum does not confirm terminal exhaustion.",
                "invalidation_or_promotion": "Promote if the correction holds local support and the next five-wave rally exceeds $1,254.81; demote on a prolonged correction proportional to the full 2012-2026 advance.",
            },
        ],
        "verdict": {
            "preferred": "Cycle I active; Primary 3 completion candidate at $1,254.81; Primary 4 may be starting; its first five-down A leg is active or attempting to bottom.",
            "confidence": {"five_wave_structures": "High", "Primary_3_top": "Moderate", "exact_Cycle_degree": "Moderate-low", "current_A_low": "Low until reversal confirmation"},
        },
    }
    return report


def render_markdown(report):
    p3 = report["preferred_high_degree_count"]["Cycle_I"]["Primary_3"]
    current = report["current_four_hour"]
    targets = report["primary_4_log_targets_if_1254_81_is_primary_3"]
    return f"""# MU Deep Elliott Wave Recount: November 2008-Present

**Symbol:** NASDAQ:MU  
**Date:** 16 July 2026  
**Status:** Pattern-first canonical working count

## Data And Scale

- 506 monthly candles, 508 weekly candles, 537 daily candles and **333 newly extracted four-hour candles**.
- Four-hour data runs from 17 March through the active 16 July bar.
- Use a **logarithmic chart** for 2008-present. MU expanded by more than 800 times, so arithmetic wave lengths distort high-degree proportionality.
- Use arithmetic Fibonacci prices for daily/four-hour execution, with percentage and log cross-checks.

## Preferred High-Degree Count

The cleanest count keeps the November 2008 advance inside an active **Cycle I**:

| Primary wave | Price path | Status |
|---|---:|---|
| Primary 1 | $1.55 -> $11.65 | Complete |
| Primary 2 | $11.65 -> $5.03 | Complete complex correction |
| Primary 3 | $5.03 -> $1,254.81 | Completion candidate |
| Primary 4 | From $1,254.81 | Active candidate |
| Primary 5 | Future | Required if this degree is correct |

Primary 2 retraced **65.54% arithmetically but only 41.64% logarithmically**. The exact internal correction is uncertain, so it is not forced into a simple ABC.

## Primary 3: 2012-2026

`$5.03 -> $35.68 -> $9.08 -> $96.24 -> $47.52 -> $1,254.81`

This passes both mandatory impulse rules. Intermediate 4 stays **$11.84** above Intermediate 1 territory, and Intermediate 3 is not shortest.

On log scale:

- Intermediate 3 is **1.205 x** Intermediate 1.
- Intermediate 5 is **1.671 x** Intermediate 1, close to a 1.618 extension.
- Intermediate 2 retraces **69.85%** on log scale.
- Intermediate 4 retraces **29.89%** on log scale.

That deep/shallow alternation is cleaner than treating the nine-month 2022 decline as an entire Cycle II. The final rise also throws over the long-term logarithmic channel, which supports a climax but does not prove completion.

## Intermediate 3

`$9.08 -> $63.05 -> $27.68 -> $94.55 -> $64.13 -> $96.24`

This is a valid normal impulse. Wave 4 holds only **$1.08** above Wave 1, but it does hold; Wave 3 is not shortest.

## Intermediate 5

`$47.52 -> $156.41 -> $61.38 -> $455.24 -> $311.44 -> $1,254.81`

This also passes both hard rules. Weekly evidence is mixed at the top: Wave 5 has the strongest EWO and volume, while RSI only slightly diverges from Wave 3. Structure permits a completed Intermediate 5, but the higher-timeframe indicators do not prove terminal exhaustion.

### Minor 3

`$61.38 -> $129.52 -> $103.21 -> $260.33 -> $192.40 -> $455.24`

- Wave 3 = **2.306 x** Wave 1.
- Wave 5 = **1.673 x** Wave 3, close to 1.618.
- No Wave 1/4 overlap.

### Minor 4

`$455.24 -> $363.69 -> $438.52 -> $311.44`

This is a strong ABC zigzag candidate. It retraced **36.51%** of Minor 3, close to 38.2%, and C measured **1.388 x A**, close to 1.382.

### Minor 5

`$311.44 -> $818.54 -> $652.11 -> $1,089.12 -> $854.22 -> $1,254.81`

This is a normal impulse, not a diagonal: Wave 4 remains above Wave 1. RSI peaks fall **85.84 -> 82.37 -> 69.77**, and EWO weakens in Wave 5. Volume does not dry up, so the lower-timeframe exhaustion signal is strong in momentum but mixed in volume.

## Current Four-Hour Structure

`$1,254.62 -> $1,119.63 -> $1,198.42 -> $875.01 -> $1,035.50 -> $851.33`

This is a valid five-down first-leg candidate. Wave 3 is approximately **2.396 x Wave 1** and is the strongest decline. Wave 5 undercuts Wave 3 while four-hour RSI and EWO are less bearish, producing bullish divergence. The active bar is not a confirmed low.

- Local correction depth: **{current['local_retracement_of_311_1254_pct']}%** of the $311.44-$1,254.81 advance.
- Local support: **$894.44**, **$783.13**, **$671.81**. The 38.2% level has broken.
- Conditional B-wave zone if $851.33 holds: **$1,005.39-$1,100.56**.
- Confirmation: five waves up, then three waves down holding above the final low.

If $1,254.81 ended Primary 3, logarithmic Primary 4 context levels are approximately **${targets['23.6_pct']}**, **${targets['38.2_pct']}**, and **${targets['50_pct']}**. These are long-duration context levels, not immediate targets.

## Alternates

1. Cycle I ended at $96.24, Cycle II ended at $47.52, and Cycle III is active. Valid, but the proposed Cycle II is unusually brief and shallow on log scale.
2. $1,254.81 ended only a lower-degree third and another fifth remains. This stays serious because weekly EWO and volume made new highs at the top.

## Verdict

**Preferred:** Cycle I active; Primary 3 may have completed at $1,254.81; Primary 4 may be beginning; its first five-down A leg is active or trying to bottom.

Confidence is high in the completed five-wave structures, moderate that $1,254.81 ended Primary 3, moderate-low in the exact Cycle label, and low that $851.33 is the final A-wave low before reversal confirmation.
"""


def main():
    report = build_report()
    serialized = json.dumps(report, indent=2, default=str)
    (ROOT / "mu_elliott_wave_analysis.json").write_text(serialized, encoding="utf-8")
    (ROOT / "mu_deep_2008_present_2026-07-16.json").write_text(serialized, encoding="utf-8")
    (ROOT / "MU_Deep_2008_Present_Report_2026-07-16.md").write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
