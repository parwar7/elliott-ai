import contextlib
import io
import json
import math
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

with contextlib.redirect_stdout(io.StringIO()):
    import analyze_rklb as base


def dt(value):
    return datetime.fromisoformat(value)


def normalize(rows):
    for index, row in enumerate(rows):
        median = (row["high"] + row["low"]) / 2
        row["ewo_pct"] = None if row["ewo"] is None else 100 * row["ewo"] / median
        start = max(0, index - 51)
        baseline = sum(item["volume"] for item in rows[start:index + 1]) / (index - start + 1)
        row["relative_volume"] = row["volume"] / baseline if baseline else None
    return rows


def metrics(rows, start, end):
    selected = [row for row in rows if dt(start) <= row["timestamp"] <= dt(end)]
    ewo = [row["ewo_pct"] for row in selected if row["ewo_pct"] is not None]
    rsi = [row["rsi"] for row in selected if row["rsi"] is not None]
    rel_volume = [row["relative_volume"] for row in selected if row["relative_volume"] is not None]
    total_volume = sum(row["volume"] for row in selected)
    return {
        "bars": len(selected),
        "cumulative_volume": round(total_volume),
        "average_volume": round(total_volume / len(selected)) if selected else None,
        "average_relative_volume": round(sum(rel_volume) / len(rel_volume), 4) if rel_volume else None,
        "ewo_peak_pct_abs": round(max((abs(value) for value in ewo), default=0), 4),
        "ewo_min_pct": round(min(ewo), 4) if ewo else None,
        "ewo_max_pct": round(max(ewo), 4) if ewo else None,
        "rsi_min": round(min(rsi), 2) if rsi else None,
        "rsi_max": round(max(rsi), 2) if rsi else None,
        "end_rsi": round(rsi[-1], 2) if rsi else None,
    }


def impulse_rules(points):
    direction = 1 if points[1] > points[0] else -1
    lengths = [
        direction * (points[1] - points[0]),
        direction * (points[3] - points[2]),
        direction * (points[5] - points[4]),
    ]
    no_overlap = points[4] > points[1] if direction == 1 else points[4] < points[1]
    return {
        "direction": "up" if direction == 1 else "down",
        "linear_lengths": {"1": round(lengths[0], 2), "3": round(lengths[1], 2), "5": round(lengths[2], 2)},
        "log_lengths": {
            "1": round(abs(math.log(points[1] / points[0])), 4),
            "3": round(abs(math.log(points[3] / points[2])), 4),
            "5": round(abs(math.log(points[5] / points[4])), 4),
        },
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": no_overlap,
        "overlap_buffer": round(direction * (points[4] - points[1]), 2),
        "mandatory_pass": lengths[1] > min(lengths[0], lengths[2]) and no_overlap,
    }


def build_report():
    monthly = normalize(base.load(ROOT / "tv_mu_monthly.json"))
    weekly = normalize(base.load(ROOT / "tv_mu_weekly.json"))
    daily = normalize(base.load(ROOT / "tv_mu_daily.json"))
    two_hour = normalize(base.load(ROOT / "tv_mu_2h.json"))

    supercycle_1 = [0.3232, 2.49, 0.6463, 46.04, 8.11, 95.08]
    cycle_1_diagonal = [1.55, 11.65, 5.03, 35.68, 9.08, 96.24]
    primary_1 = [47.52, 73.80, 59.72, 129.61, 104.97, 156.41]
    primary_2_complex = [156.41, 84.38, 114.08, 83.11, 110.25, 61.38]
    nested_pairs = {
        "Intermediate": [61.38, 129.52, 103.21],
        "Minor": [103.21, 260.33, 192.40],
        "Minute": [192.40, 455.24, 311.44],
    }
    nested_minute_3 = [311.44, 818.54, 652.11, 1089.12, 854.22, 1254.81]
    current_a = [1254.81, 1119.63, 1198.42, 875.01, 1035.50, 873.63]

    primary_1_metrics = {
        "1": metrics(weekly, "2022-09-19", "2023-05-30T23:59:59"),
        "2": metrics(weekly, "2023-05-30", "2023-07-03T23:59:59"),
        "3": metrics(weekly, "2023-07-03", "2024-04-01T23:59:59"),
        "4": metrics(weekly, "2024-04-01", "2024-04-15T23:59:59"),
        "5": metrics(weekly, "2024-04-15", "2024-06-17T23:59:59"),
    }

    nested_minute_3_metrics = {
        "1": metrics(daily, "2026-03-31", "2026-05-11T23:59:59"),
        "2": metrics(daily, "2026-05-11", "2026-05-19T23:59:59"),
        "3": metrics(daily, "2026-05-19", "2026-06-03T23:59:59"),
        "4": metrics(daily, "2026-06-03", "2026-06-09T23:59:59"),
        "5": metrics(daily, "2026-06-09", "2026-06-25T23:59:59"),
    }
    current_a_metrics = {
        "i": metrics(two_hour, "2026-06-25T12:00", "2026-06-26T12:00"),
        "ii": metrics(two_hour, "2026-06-26T12:00", "2026-06-26T14:00"),
        "iii": metrics(two_hour, "2026-06-26T14:00", "2026-07-08T08:00"),
        "iv": metrics(two_hour, "2026-07-08T08:00", "2026-07-09T12:00"),
        "v": metrics(two_hour, "2026-07-09T12:00", "2026-07-15T20:00"),
    }

    cycle_2_a_log = abs(math.log(6.44 / 95.08))
    cycle_2_c_log = abs(math.log(1.55 / 18.19))
    first_wave_logs = {
        degree: round(math.log(points[1] / points[0]), 4)
        for degree, points in nested_pairs.items()
    }
    second_wave_retracements = {
        degree: round((points[1] - points[2]) / (points[1] - points[0]) * 100, 2)
        for degree, points in nested_pairs.items()
    }

    return {
        "symbol": "NASDAQ:MU",
        "company": "Micron Technology, Inc.",
        "analysis_date": "2026-07-16",
        "method": "Pattern-first blind recount: pivots and geometry, scale selection, Fibonacci, RSI/EWO, volume, then degree assignment",
        "analysis_policy": {
            "order": [
                "Identify objective pivots without labels",
                "Classify impulse, correction, diagonal, triangle, or channel geometry",
                "Select arithmetic or logarithmic scale from duration and percentage expansion",
                "Test Fibonacci retracements and extensions",
                "Use RSI, EWO, and volume as confirmation or contradiction",
                "Assign the Elliott degree only after proportionality across adjacent waves is established",
            ],
            "no_forced_nesting": True,
            "scale_policy": {
                "monthly_multi_cycle": "Logarithmic, because MU spans decades and multiplicative price changes.",
                "weekly_primary": "Check both; use log for proportionality and arithmetic for local pivot targets.",
                "daily_intraday": "Arithmetic for nearby retracements/extensions, with a log cross-check when a move exceeds 100%.",
            },
        },
        "data": {
            "source": "Live TradingView table view",
            "monthly": {"rows": len(monthly), "start": monthly[0]["timestamp"].isoformat(), "end": monthly[-1]["timestamp"].isoformat()},
            "weekly": {"rows": len(weekly), "start": weekly[0]["timestamp"].isoformat(), "end": weekly[-1]["timestamp"].isoformat()},
            "daily": {"rows": len(daily), "start": daily[0]["timestamp"].isoformat(), "end": daily[-1]["timestamp"].isoformat()},
            "two_hour": {"rows": len(two_hour), "start": two_hour[0]["timestamp"].isoformat(), "end": two_hour[-1]["timestamp"].isoformat()},
            "last_regular_close": 904.28,
            "last_two_hour_close": 902.12,
            "current_low": 873.63,
        },
        "highest_degree_context": {
            "Supercycle_I": {
                "path": [0.3232, 95.08],
                "dates": ["1986-12", "2000-07"],
                "internal_points": supercycle_1,
                "rules": impulse_rules(supercycle_1),
                "status": "Completed standard five-wave impulse candidate",
            },
            "Supercycle_II": {
                "path": [95.08, 1.55],
                "dates": ["2000-07", "2008-11"],
                "structure": {"A": 6.44, "B": 18.19, "C": 1.55},
                "A_log_distance": round(cycle_2_a_log, 4),
                "C_log_distance": round(cycle_2_c_log, 4),
                "C_to_A_log_ratio": round(cycle_2_c_log / cycle_2_a_log, 4),
                "status": "Completed ABC zigzag candidate with near log-scale A/C equality",
            },
            "Supercycle_III": {
                "status": "Active",
                "Cycle_I": {
                    "path": [1.55, 96.24],
                    "dates": ["2008-11", "2022-01"],
                    "internal_points": cycle_1_diagonal,
                    "classification": "Expanding leading diagonal candidate",
                    "reason": "Wave 4 overlaps Wave 1, so this cannot be stored as a standard impulse; highs and corrective lows expand/rise consistently.",
                    "confidence": "Low-moderate: the overlap permits a diagonal, but the required internal subdivisions cannot be proven with the available pre-2016 lower-timeframe history.",
                    "alternate": {
                        "classification": "Nested Cycle-degree 1-2 acceleration candidate",
                        "pairs": [[1.55, 11.65, 5.03], [5.03, 35.68, 9.08]],
                        "reason": "Two rising 1-2 pairs avoid forcing a rare bullish expanding diagonal, but they do not prove that Cycle I ended at $96.24.",
                    },
                },
                "Cycle_II": {"path": [96.24, 47.52], "dates": ["2022-01", "2022-09"], "retracement_pct": 50.58},
                "Cycle_III": {"path": [47.52, "active"], "dates": ["2022-09", "present"]},
            },
        },
        "preferred_count": {
            "name": "Cycle III active; five-wave advance completed at $1,254.81; degree scenario unresolved; correction active",
            "Cycle_III": {
                "Primary_1": {
                    "path": [47.52, 156.41],
                    "dates": ["2022-09", "2024-06"],
                    "internal_points": primary_1,
                    "internal_dates": ["2022-09-19", "2023-05-30", "2023-07-03", "2024-04-01", "2024-04-15", "2024-06-17"],
                    "rules": impulse_rules(primary_1),
                    "metrics": primary_1_metrics,
                    "indicator_verdict": {
                        "wave_3_positive_ewo_above_wave_1": primary_1_metrics["3"]["ewo_max_pct"] > primary_1_metrics["1"]["ewo_max_pct"],
                        "wave_3_rsi_above_wave_1": primary_1_metrics["3"]["rsi_max"] > primary_1_metrics["1"]["rsi_max"],
                        "wave_4_volume_dry_up": primary_1_metrics["4"]["average_relative_volume"] < primary_1_metrics["3"]["average_relative_volume"],
                        "wave_5_ewo_divergence": primary_1_metrics["5"]["ewo_max_pct"] < primary_1_metrics["3"]["ewo_max_pct"],
                        "summary": "The five-wave price structure passes both mandatory impulse rules. Wave 3 improves on Wave 1 in positive EWO and RSI, but Wave 4 volume expands and Wave 5 has no EWO divergence, so the indicators are supportive but not textbook-perfect.",
                    },
                    "status": "Completed five-wave impulse; price confidence high, indicator confirmation mixed",
                },
                "Primary_2": {
                    "path": [156.41, 61.38],
                    "dates": ["2024-06", "2025-04"],
                    "retracement_pct": 87.27,
                    "internal_points": primary_2_complex,
                    "classification": "Complex correction candidate (W-X-Y or W-X-Y-X-Z)",
                    "reason": "The weekly path contains multiple alternating declines and rebounds rather than one independently proven 5-3-5 zigzag.",
                    "status": "Complete by price and origin hold; exact corrective family unresolved",
                },
                "Primary_3": {
                    "status": "Active",
                    "Intermediate_1": {"path": [61.38, 129.52], "dates": ["2025-04", "2025-06"]},
                    "Intermediate_2": {"path": [129.52, 103.21], "dates": ["2025-06", "2025-08"], "retracement_pct": 38.61},
                    "Intermediate_3": {
                        "status": "Active",
                        "Minor_1": {"path": [103.21, 260.33], "dates": ["2025-08", "2025-11"]},
                        "Minor_2": {"path": [260.33, 192.40], "dates": ["2025-11", "2025-11"], "retracement_pct": 43.22},
                        "Minor_3": {
                            "status": "Active",
                            "Minute_1": {"path": [192.40, 455.24], "dates": ["2025-11", "2026-01"]},
                            "Minute_2": {"path": [455.24, 311.44], "dates": ["2026-01", "2026-03"], "retracement_pct": 54.71},
                            "Minute_3": {
                                "path": [311.44, 1254.81],
                                "dates": ["2026-03", "2026-06"],
                                "internal_points": nested_minute_3,
                                "rules": impulse_rules(nested_minute_3),
                                "metrics": nested_minute_3_metrics,
                                "indicator_verdict": {
                                    "normalized_ewo_wave_3_above_wave_1": nested_minute_3_metrics["3"]["ewo_peak_pct_abs"] > nested_minute_3_metrics["1"]["ewo_peak_pct_abs"],
                                    "rsi_wave_3_above_wave_1": nested_minute_3_metrics["3"]["rsi_max"] > nested_minute_3_metrics["1"]["rsi_max"],
                                    "correction_volume_dry_up": nested_minute_3_metrics["2"]["average_relative_volume"] < nested_minute_3_metrics["1"]["average_relative_volume"] and nested_minute_3_metrics["4"]["average_relative_volume"] < nested_minute_3_metrics["3"]["average_relative_volume"],
                                    "wave_5_rsi_ewo_divergence": nested_minute_3_metrics["5"]["rsi_max"] < nested_minute_3_metrics["3"]["rsi_max"] and nested_minute_3_metrics["5"]["ewo_peak_pct_abs"] < nested_minute_3_metrics["3"]["ewo_peak_pct_abs"],
                                    "summary": "Price rules and normalized EWO support the nested third. RSI did not strengthen from Wave 1 to Wave 3 and corrective volume did not dry up, so indicator confirmation is mixed. Wave 5 has clear RSI/EWO divergence.",
                                },
                                "status": "Completed five-wave third-wave impulse",
                            },
                            "Minute_4": {"path": [1254.81, "active"], "last_closed_data_low": 873.63, "live_observed_low": 866.12, "status": "Active; first A-leg remains incomplete/unconfirmed"},
                            "Minute_5": {"status": "Expected after Minute 4"},
                        },
                        "Minor_4": {"status": "Future after Minor 3 completes"},
                        "Minor_5": {"status": "Future after Minor 4"},
                    },
                    "Intermediate_4": {"status": "Future after Intermediate 3 completes"},
                    "Intermediate_5": {"status": "Future after Intermediate 4"},
                },
            },
            "nested_one_two_evidence": {
                "pairs": nested_pairs,
                "first_wave_log_distances": first_wave_logs,
                "second_wave_retracements_pct": second_wave_retracements,
                "origin_holds": {
                    "Intermediate_2_above_Intermediate_1_origin": 103.21 > 61.38,
                    "Minor_2_above_Minor_1_origin": 192.40 > 103.21,
                    "Minute_2_above_Minute_1_origin": 311.44 > 192.40,
                },
            },
            "selection_reason": [
                "The vertical $311.44-$1,254.81 advance independently subdivides into five and passes the mandatory price rules.",
                "EWO peaks in Wave 3 while RSI is strongest in Wave 1; both weaken into Wave 5, supporting exhaustion but not fixing the degree.",
                "Three earlier motive/corrective pairs make nesting plausible, but nesting is a scenario rather than a required conclusion.",
                "The next correction depth, channel behavior, and rebound structure must determine whether $1,254.81 ended Minute 3, Intermediate 3, or a larger impulse.",
            ],
            "pattern_first_assessment": {
                "confirmed_pattern": "Completed five-wave impulse from $311.44 to $1,254.81",
                "fibonacci_relationships": {
                    "wave_3_to_wave_1_linear": 0.862,
                    "wave_5_to_wave_1_linear": 0.790,
                    "wave_5_to_wave_3_linear": 0.917,
                    "interpretation": "The motive waves contract rather than show a 1.618 extension; this is compatible with a completed balanced impulse and does not itself prove third-wave nesting.",
                },
                "momentum_volume": {
                    "RSI_peaks_1_3_5": [85.84, 82.37, 69.77],
                    "EWO_peaks_pct_1_3_5": [27.3856, 30.6251, 25.0703],
                    "average_relative_volume_1_3_5": [1.0863, 1.0701, 1.1562],
                    "interpretation": "RSI and EWO diverge into Wave 5, but average relative volume does not dry up. Momentum supports exhaustion; volume confirmation is mixed.",
                },
                "degree_scenarios": [
                    {
                        "label": "Minute 3 ended; Minute 4 active",
                        "confidence": "Moderate working scenario",
                        "confirmation": "Correction holds above $455.24 and is followed by a five-wave advance beyond $1,254.81.",
                    },
                    {
                        "label": "Intermediate 3 ended; Intermediate 4 active",
                        "confidence": "Moderate alternate",
                        "confirmation": "A broader correction develops and the rebound fails to produce the expected nested Minute/Minor fifth-wave sequence.",
                    },
                    {
                        "label": "Larger impulse ended at $1,254.81",
                        "confidence": "Lower-confidence alternate",
                        "confirmation": "Price breaks the nested supports and forms a correction proportional to the full advance from $61.38 or $47.52.",
                    },
                ],
            },
        },
        "current_correction": {
            "degree": "Correction after the completed $311.44-$1,254.81 five; Minute 4 is the working degree, not a confirmed fact",
            "current_leg": "Wave A five-down candidate",
            "points": current_a,
            "rules": impulse_rules(current_a),
            "metrics": current_a_metrics,
            "divergence": {
                "price": "Wave v marginally undercut Wave iii: $873.63 vs $875.01.",
                "rsi": "Two-hour RSI is materially stronger than at Wave iii, a bullish divergence candidate.",
                "ewo": "Two-hour normalized/raw EWO is materially less negative than at Wave iii, a bullish divergence candidate.",
                "volume": "Volume does not provide a strict standalone terminal confirmation.",
            },
            "status": "Still active and unconfirmed. The July 16 live low of $866.12 undercut the last closed-data pivot, and no five-up reversal plus three-down hold has formed.",
            "closed_data_through": "2026-07-15",
            "live_observation": {
                "date": "2026-07-16",
                "low": 866.12,
                "note": "Intraday observation only; excluded from RSI, EWO, and volume calculations until the bar closes and data is refreshed.",
                "updated_minute_4_retracement_pct": 41.20,
                "conditional_B_zone_if_A_ends_here": [1014.60, 1106.33],
            },
            "probable_B_retracement_zone": [1019.24, 1109.20],
            "Minute_4_retracement_zones": {
                "38.2_pct_of_Minute_3": 894.46,
                "50_pct_of_Minute_3": 783.13,
                "61.8_pct_of_Minute_3": 671.79,
            },
        },
        "alternates": [
            {
                "name": "Intermediate 3 completed at $1,254.81; Intermediate 4 active",
                "status": "Valid less-bullish alternate",
                "weakness": "It compresses the three clean 1-2 pairs into one completed five and makes the final actionary wave dominate the count.",
                "promotion_trigger": "Promote if price breaks below $455.24 or the rebound fails to form the expected Minute 5 sequence.",
            },
            {
                "name": "Primary 3 completed at $1,254.81; Primary 4 active",
                "status": "Valid higher-degree alternate",
                "weakness": "The correction is still too young to prove Primary-degree duration and complexity.",
                "promotion_trigger": "Promote if a multi-month Primary-degree correction develops and the nested count fails.",
            },
        ],
        "decision_levels": {
            "current_price_area": [902.12, 904.28],
            "current_low": 873.63,
            "early_reversal_levels": [997.26, 1035.50],
            "probable_B_zone": [1019.24, 1109.20],
            "Minute_4_support": [894.46, 783.13, 671.79],
            "nested_count_hard_invalidation": "Below $455.24 overlaps Minor 3 Minute 1 and invalidates the preferred standard nested impulse at that level.",
            "bullish_confirmation": "Five waves up from the final low, then three waves down that remain above that low.",
            "trade_stance": "No confirmed long entry and no attractive fresh short after the vertical decline; wait for structure.",
        },
        "verdict": {
            "preferred_hierarchy": "Supercycle III > Cycle III > Primary 3 > Intermediate 3 > Minor 3 > Minute 4 active > Wave A possibly completing",
            "structural_confidence": "High for Primary 1 and nested Minute 3 hard price rules; moderate for the nested degree hierarchy; low-moderate for exact Supercycle/Cycle labels.",
            "low_confidence": "The exact Cycle I pattern and Primary 2 corrective family are unresolved. No final A-wave or Minute 4 low is confirmed.",
        },
    }


def render_markdown(report):
    preferred = report["preferred_count"]
    p3 = preferred["Cycle_III"]["Primary_3"]
    return f"""# MU Blind Elliott Wave Recount

**Symbol:** NASDAQ:MU  
**Date:** 16 July 2026  
**Status:** Re-audited canonical working count

## Data

- {report['data']['monthly']['rows']} monthly candles from June 1984.
- {report['data']['weekly']['rows']} weekly candles from October 2016.
- {report['data']['daily']['rows']} daily candles through 15 July 2026.
- {report['data']['two_hour']['rows']} two-hour candles through 15 July 2026.
- July 16 intraday observation: **$866.12 low**. It is not included in closed-bar indicator calculations.

## Method

This is a pattern-first count. Pivots and chart geometry are identified before Elliott degree. The monthly chart is evaluated logarithmically; weekly structure is checked on both scales; daily and intraday Fibonacci levels use arithmetic prices with a logarithmic cross-check for very large moves. RSI/EWO, Fibonacci, volume, and channel behavior confirm or challenge a pattern but do not create the count.

## Highest-Degree Map

- Supercycle I: **$0.3232 to $95.08** (1986-2000), standard five-wave candidate.
- Supercycle II: **$95.08 to $1.55** (2000-2008), ABC zigzag candidate. A/C log-distance ratio is **{report['highest_degree_context']['Supercycle_II']['C_to_A_log_ratio']}**.
- Supercycle III: active from **$1.55**.
- Cycle I: **$1.55 to $96.24**, expanding leading-diagonal candidate, but only low-moderate confidence.
- Cycle II: **$96.24 to $47.52**.
- Cycle III: active from **$47.52**.

Cycle I cannot be a standard impulse because its proposed Wave 4 overlaps Wave 1. A diagonal is permitted, but its internal subdivisions cannot be proved from the available lower-timeframe history. A nested 1-2 acceleration from **$1.55 -> $11.65 -> $5.03 -> $35.68 -> $9.08** remains the main high-degree alternate.

## Neutral Structural Count

### Cycle III

| Primary wave | Price path | Status |
|---|---:|---|
| Primary 1 | $47.52 to $156.41 | Complete five-wave impulse |
| Primary 2 | $156.41 to $61.38 | Complete; exact complex family unresolved |
| Primary 3 | $61.38 to active | Active |

### Primary 1 Audit

`$47.52 -> $73.80 -> $59.72 -> $129.61 -> $104.97 -> $156.41`

Wave 3 is not shortest and Wave 4 stays **$31.17** above the Wave 1 high, so both mandatory impulse rules pass. Wave 3 reaches approximately **2.659 x Wave 1**, very close to the **2.618 extension target near $128.52**. Wave 5 is approximately **1.957 x Wave 1**. Positive EWO and RSI are stronger in Wave 3 than Wave 1. Wave 4 volume expands and Wave 5 has no EWO divergence, so indicator confirmation is supportive but not textbook-perfect.

### Primary 2 Audit

`$156.41 -> $84.38 -> $114.08 -> $83.11 -> $110.25 -> $61.38`

This is better stored as a **complex correction candidate**, not a proven ABC. The repeated decline/rebound legs support W-X-Y or W-X-Y-X-Z, but the exact family cannot be determined confidently from the current aggregation. It retraced **87.27%** of Primary 1 without breaking the **$47.52** origin.

### Possible 1-2 Relationships Inside Primary 3

| Degree | Wave 1 | Wave 2 | Retracement |
|---|---:|---:|---:|
| Intermediate | $61.38 to $129.52 | $129.52 to $103.21 | 38.61% |
| Minor | $103.21 to $260.33 | $260.33 to $192.40 | 43.22% |
| Minute | $192.40 to $455.24 | $455.24 to $311.44 | 54.71% |

Every pullback remains above its preceding motive origin. Their logarithmic distances are **{preferred['nested_one_two_evidence']['first_wave_log_distances']['Intermediate']}**, **{preferred['nested_one_two_evidence']['first_wave_log_distances']['Minor']}**, and **{preferred['nested_one_two_evidence']['first_wave_log_distances']['Minute']}**. This makes a nested interpretation possible, but these labels are not imposed before the larger pattern is confirmed.

The third-of-a-third advance from $311.44 subdivides cleanly:

`$311.44 -> $818.54 -> $652.11 -> $1,089.12 -> $854.22 -> $1,254.81`

This is the strongest confirmed lower-timeframe fact: it is a valid completed five-wave impulse. Its motive-wave length ratios are **0.862** for Wave 3/Wave 1 and **0.790** for Wave 5/Wave 1. RSI peaks decline **85.84 -> 82.37 -> 69.77**, while EWO peaks **27.39% -> 30.63% -> 25.07%**. That supports Wave 5 exhaustion. Average relative volume does not decline into Wave 5, so volume confirmation is mixed.

The degree remains conditional:

- Scenario A: Minute 3 ended and Minute 4 is active.
- Scenario B: Intermediate 3 ended and Intermediate 4 is active.
- Scenario C: a larger impulse ended at $1,254.81.

Scenario A is the working label, not a forced conclusion. The depth and duration of this correction, its channel, and the structure of the next rally will choose among them.

## Current Correction

The decline from $1,254.81 is a five-down Wave A candidate:

Closed-bar candidate: `$1,254.81 -> $1,119.63 -> $1,198.42 -> $875.01 -> $1,035.50 -> $873.63`

That sequence passes the mandatory bearish impulse rules and showed RSI/EWO divergence. However, the July 16 live low reached **$866.12**, invalidating $873.63 as a completed pivot. Wave A.v therefore remains active until reversal structure appears.

- Early reversal levels: **$997.26**, then **$1,035.50**.
- Conditional B-wave zone if A ends at $866.12: **$1,014.60-$1,106.33**.
- Minute 4 retracement levels: **$894.46**, **$783.13**, then **$671.79**.
- Hard invalidation for this nested level: **below $455.24**, which would overlap Minute 1 inside Minor 3.

## Alternates

1. Intermediate 3 completed at $1,254.81 and Intermediate 4 is active. This is valid but compresses three clean 1-2 pairs into one completed impulse.
2. Primary 3 completed at $1,254.81 and Primary 4 is active. This is valid but the correction is not yet old or complex enough to prove Primary degree.

## Verdict

**Confirmed pattern:** a five-wave advance completed from **$311.44 to $1,254.81**, followed by an active five-down corrective leg.

**Working degree:** Minute 3 complete and Minute 4 active, with Intermediate 4 and a larger completed impulse retained as real alternatives. Confidence is high in the five-wave pattern and only moderate in its exact degree. Confirmation still requires five waves up from the final low followed by a three-wave pullback that holds above it.
"""


def main():
    report = build_report()
    serialized = json.dumps(report, indent=2)
    (ROOT / "mu_elliott_wave_analysis.json").write_text(serialized, encoding="utf-8")
    (ROOT / "mu_blind_recount_2026-07-16.json").write_text(serialized, encoding="utf-8")
    (ROOT / "MU_Blind_Recount_Report_2026-07-16.md").write_text(render_markdown(report), encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
