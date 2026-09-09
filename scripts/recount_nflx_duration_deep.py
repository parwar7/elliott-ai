import json
import math
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def d(value):
    return datetime.fromisoformat(value)


def years(start, end):
    return round((d(end) - d(start)).days / 365.25, 2)


def log_length(start, end):
    return round(math.log(end / start), 3)


def impulse(points):
    one = points[1] - points[0]
    three = points[3] - points[2]
    five = points[5] - points[4]
    return {
        "wave_3_not_shortest": three > min(one, five),
        "wave_4_no_overlap": points[4] > points[1],
        "arithmetic_lengths": [round(one, 4), round(three, 4), round(five, 4)],
        "log_lengths": [log_length(points[0], points[1]), log_length(points[2], points[3]), log_length(points[4], points[5])],
    }


def main():
    weekly = json.loads((ROOT / "tv_nflx_weekly_complete.json").read_text(encoding="utf-8"))
    dates = [datetime.strptime(row["date"], "%a %d %b '%y") for row in weekly]
    report = {
        "symbol": "NASDAQ:NFLX",
        "analysis_date": "2026-07-17",
        "latest_bar": dates[0].date().isoformat(),
        "method": "Full-history Elliott recount using logarithmic price geometry, duration priors, Fibonacci relationships, and previously audited RSI/EWO/volume evidence.",
        "duration_principle": "Duration is a soft prior. It can downgrade a degree label, but it cannot override a clean price subdivision and confirmed invalidation levels.",
        "data": {"weekly_bars": len(weekly), "start": dates[-1].date().isoformat(), "end": dates[0].date().isoformat()},
        "high_degree_comparison": {
            "preferred_2025_top": {
                "pivots": [0.0447, 4.25, 0.7544, 69.10, 16.43, 134.12],
                "durations_years": [8.77, 1.31, 9.34, 0.66, 3.05],
                "motive_log_lengths": [4.555, 4.517, 2.1],
                "price_rules": impulse([0.0447, 4.25, 0.7544, 69.10, 16.43, 134.12]),
                "decision": "Preferred. Waves 1 and 3 are nearly equal in log distance, while wave 5 is materially shorter and terminal. The timing also shows the long middle advance followed by a shorter final advance, which is coherent with a mature impulse.",
            },
            "alternate_2021_top": {
                "pivots": [0.0447, 0.5657, 0.1273, 4.25, 0.7544, 69.10],
                "durations_years": [4.75, 0.91, 8.79, 0.95, 9.34],
                "motive_log_lengths": [2.538, 3.508, 4.517],
                "price_rules": impulse([0.0447, 0.5657, 0.1273, 4.25, 0.7544, 69.10]),
                "decision": "Valid alternate, downgraded. The final motive wave is the largest on log distance and also lasts about as long as the preceding wave, requiring a progressively extended terminal wave.",
            },
            "degree_warning": "Both 2002-to-2021 and 2002-to-2025 spans exceed the supplied 3-to-7-year Cycle prior. Therefore Cycle I is a working label; the whole move may be better described as a higher-degree Supercycle segment containing Cycle-like subdivisions.",
        },
        "preferred_hierarchy": {
            "working_count": "Cycle I / higher-degree motive segment -> Primary 1-5 complete at 134.12 in June 2025",
            "primary_waves": {
                "1": {"price": [0.0447, 4.25], "dates": ["2002-09", "2011-07"], "duration_years": 8.77},
                "2": {"price": [4.25, 0.7544], "dates": ["2011-07", "2012-07"], "duration_years": 1.31, "family": "deep sharp/complex correction candidate"},
                "3": {"price": [0.7544, 69.10], "dates": ["2012-07", "2021-10"], "duration_years": 9.34, "duration_note": "longest and strongest price advance; RSI/EWO peak evidence supports wave 3"},
                "4": {"price": [69.10, 16.43], "dates": ["2021-10", "2022-06"], "duration_years": 0.66, "family": "complex W-X-Y candidate"},
                "5": {"price": [16.43, 134.12], "dates": ["2022-06", "2025-06"], "duration_years": 3.05, "duration_note": "shorter in log distance and time than wave 3; not an abnormal extension"},
            },
            "primary_5_intermediate": {
                "1": [16.43, 45.65],
                "2": [45.65, 35.21],
                "3": [35.21, 106.45],
                "4": [106.45, 85.39],
                "5": [85.39, 134.12],
                "verification": "Wave 3 is not shortest, wave 4 does not overlap wave 1, and prior EWO/RSI evidence favored a completed impulse at 134.12.",
            },
            "primary_2_active": {
                "A": {"price": [134.12, 75.23], "shape": "five-wave decline"},
                "B": {"price": [75.23, 108.95], "shape": "three-wave zigzag candidate"},
                "C": {"price": [108.95, 72.28], "shape": "active decline; lower-degree five-wave candidate"},
                "lower_degree_C": [108.95, 91.30, 94.22, 70.86, 78.44, 72.28],
                "minute_inside_final_leg": [78.44, 75.72, 78.18, 72.52, 75.39, 72.28],
            },
        },
        "confirmation": {
            "supporting": [
                "Primary 1, 3, and 5 pass the mandatory wave-3 and wave-4 price rules.",
                "Primary 3 produced stronger weekly RSI and normalized EWO than Primary 5, supporting terminal divergence at the 2025 top.",
                "Primary C has a completed-looking five-wave lower-degree decline into the 70.86-72.56 zone.",
                "The 2025 primary-5 duration is shorter than primary-3 duration, consistent with a terminal wave after a strong extended middle wave.",
            ],
            "not_confirmed": [
                "The exact Cycle versus Supercycle degree name is not provable from price alone.",
                "72.28 is not confirmed as the final Primary 2 low until a five-wave rally and a three-wave pullback hold above it.",
                "Volume is supportive context but not a strict standalone label because normalized relative volume did not show clean terminal divergence.",
            ],
        },
        "levels": {
            "near_term_invalidation": 78.44,
            "primary_C_invalidation": 108.95,
            "primary_2_invalidation": 134.12,
            "first_reversal_confirmation": 78.44,
            "stronger_reversal_confirmation": 94.22,
            "structural_confirmation": 108.95,
            "downside_confluence": [67.08, 67.53, 60.79, 62.66, 50.06, 51.26],
        },
        "verdict": "The best current count remains a completed five-wave advance from 0.0447 to 134.12, followed by Primary 2 A-B-C with C still active or recently attempting a low near 72.28. Duration analysis strengthens the 2025 top versus the 2021 top, but lowers confidence in the exact Cycle label; the geometry is more reliable than the degree name.",
    }
    (ROOT / "nflx_duration_deep_reaudit_2026-07-17.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = """# NFLX Deep Duration Recount - 17 July 2026

## Verdict

The best working count remains a completed five-wave advance from **$0.0447 to $134.12**, followed by an active **Primary 2** correction. The duration check strengthens the 2025 top over the 2021 top, but the exact Cycle/Supercycle name remains provisional because the entire 2002-to-2025 advance is longer than the supplied 3-to-7-year Cycle guideline.

## Why The 2025 Top Is Preferred

| Wave | Price | Approx. duration |
|---|---:|---:|
| Primary 1 | $0.0447 -> $4.25 | 8.77 years |
| Primary 2 | $4.25 -> $0.7544 | 1.31 years |
| Primary 3 | $0.7544 -> $69.10 | 9.34 years |
| Primary 4 | $69.10 -> $16.43 | 0.66 years |
| Primary 5 | $16.43 -> $134.12 | 3.05 years |

Primary 1 and Primary 3 are nearly equal on logarithmic price distance. Primary 5 is shorter in both log distance and time, so it is not the extreme extension seen in the 2021-ending alternate. The 2021 alternate makes its final motive wave the largest and longest, which is less parsimonious.

## Internal Count

Primary 5 subdivides as:

- Intermediate 1: **$16.43 -> $45.65**
- Intermediate 2: **$45.65 -> $35.21**
- Intermediate 3: **$35.21 -> $106.45**
- Intermediate 4: **$106.45 -> $85.39**
- Intermediate 5: **$85.39 -> $134.12**

The current correction counts as:

- Primary 2 A: **$134.12 -> $75.23**, five-wave decline
- Primary 2 B: **$75.23 -> $108.95**, three-wave zigzag candidate
- Primary 2 C: **$108.95 -> $72.28**, active or attempting a low

Inside C, the working lower-degree path is **$108.95 -> $91.30 -> $94.22 -> $70.86 -> $78.44 -> $72.28**. The final decline currently subdivides **$78.44 -> $75.72 -> $78.18 -> $72.52 -> $75.39 -> $72.28**.

## Confirmation And Risk

RSI and normalized EWO previously supported a terminal divergence at the 2025 top: Primary 3 had stronger momentum than Primary 5. Volume is only contextual because normalized relative volume was neutral. The price rules pass for the audited impulses.

The first reversal signal is above **$78.44**; stronger confirmation is above **$94.22**; structural confirmation is above **$108.95**. A move above **$134.12** invalidates Primary 2 as still active. Downside confluence sits near **$67.08-$67.53**, **$60.79-$62.66**, and **$50.06-$51.26**.

The low near **$72.28 is not confirmed** until a five-wave rally is followed by a three-wave pullback that holds above it.
"""
    (ROOT / "NFLX_Deep_Duration_Recount_Report_2026-07-17.md").write_text(md, encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
