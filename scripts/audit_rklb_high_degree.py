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


def add_normalized_indicators(rows):
    for index, row in enumerate(rows):
        median = (row["high"] + row["low"]) / 2
        row["ewo_pct"] = None if row["ewo"] is None else 100 * row["ewo"] / median
        start = max(0, index - 51)
        baseline = sum(item["volume"] for item in rows[start:index + 1]) / (index - start + 1)
        row["relative_volume_52"] = row["volume"] / baseline if baseline else None
    return rows


def metrics(rows, start, end):
    selected = [row for row in rows if dt(start) <= row["timestamp"] <= dt(end)]
    ewo_pct = [row["ewo_pct"] for row in selected if row["ewo_pct"] is not None]
    rsi = [row["rsi"] for row in selected if row["rsi"] is not None]
    relative_volume = [row["relative_volume_52"] for row in selected if row["relative_volume_52"] is not None]
    volume = sum(row["volume"] for row in selected)
    return {
        "bars": len(selected),
        "cumulative_volume": round(volume),
        "average_volume": round(volume / len(selected)) if selected else None,
        "average_relative_volume_52": round(sum(relative_volume) / len(relative_volume), 4) if relative_volume else None,
        "ewo_peak_pct_abs": round(max((abs(value) for value in ewo_pct), default=0), 4),
        "ewo_min_pct": round(min(ewo_pct), 4) if ewo_pct else None,
        "ewo_max_pct": round(max(ewo_pct), 4) if ewo_pct else None,
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
        "linear_lengths": {"1": round(lengths[0], 2), "3": round(lengths[1], 2), "5": round(lengths[2], 2)},
        "log_lengths": {
            "1": round(math.log(points[1] / points[0]), 4),
            "3": round(math.log(points[3] / points[2]), 4),
            "5": round(math.log(points[5] / points[4]), 4),
        },
        "wave_3_not_shortest": lengths[1] > min(lengths[0], lengths[2]),
        "wave_4_no_overlap": no_overlap,
        "overlap_buffer": round(direction * (points[4] - points[1]), 2),
    }


def build_report():
    weekly = add_normalized_indicators(base.load(ROOT / "tv_rklb_weekly.json"))
    two_hour = add_normalized_indicators(base.load(ROOT / "tv_rklb_2h.json"))

    intermediate_1 = [3.47, 5.84, 4.20, 28.05, 21.87, 33.34]
    intermediate_3 = [14.71, 53.44, 38.26, 99.58, 56.13, 151.00]
    primary_candidate = [3.47, 33.34, 14.71, 151.00]
    parent_log_lengths = [
        round(math.log(primary_candidate[1] / primary_candidate[0]), 4),
        round(math.log(primary_candidate[3] / primary_candidate[2]), 4),
    ]

    i3_metrics = {
        "Minor_1": metrics(weekly, "2025-04-07", "2025-07-20T23:59:59"),
        "Minor_2": metrics(weekly, "2025-07-14", "2025-08-24T23:59:59"),
        "Minor_3": metrics(weekly, "2025-08-18", "2026-01-18T23:59:59"),
        "Minor_4": metrics(weekly, "2026-01-12", "2026-04-05T23:59:59"),
        "Minor_5": metrics(weekly, "2026-03-30", "2026-05-31T23:59:59"),
    }

    correction_metrics = {
        "A": metrics(two_hour, "2026-05-27T08:00", "2026-06-25T18:00"),
        "B": metrics(two_hour, "2026-06-25T18:00", "2026-07-01T16:00"),
        "C_candidate": metrics(two_hour, "2026-07-01T16:00", "2026-07-15T08:00"),
    }
    live_price = 76.62
    live_low = 74.84
    c_subwaves = [107.58, 97.91, 102.53, 80.51, 88.38, live_low]
    last = two_hour[-1]

    return {
        "symbol": "NASDAQ:RKLB",
        "audit_date": "2026-07-15",
        "data": {
            "source": "TradingView table exports in workspace",
            "weekly_rows": len(weekly),
            "weekly_start": weekly[0]["timestamp"].isoformat(),
            "weekly_end": weekly[-1]["timestamp"].isoformat(),
            "two_hour_rows": len(two_hour),
            "two_hour_end": two_hour[-1]["timestamp"].isoformat(),
            "last_observation": {"close": last["close"], "rsi": round(last["rsi"], 2), "ewo_pct": round(last["ewo_pct"], 4)},
            "live_chart_check": {"time_utc": "2026-07-15T19:38:00", "price": live_price, "visible_session_low": live_low},
        },
        "degree_limit": "RKLB history is too short to establish Cycle degree reliably; Primary is an operational degree label.",
        "preferred_count": {
            "name": "Primary 1 active; Intermediate (4) active or attempting a low",
            "Primary_1": {
                "start": ["2024-04-15", 3.47],
                "Intermediate_1": [3.47, 33.34],
                "Intermediate_2": [33.34, 14.71],
                "Intermediate_3": [14.71, 151.00],
                "Intermediate_4": [151.00, live_low, "active/unconfirmed low"],
                "Intermediate_5": ["future", "required before Primary 1 can be considered complete"],
            },
            "parent_log_comparison": {
                "Intermediate_1": parent_log_lengths[0],
                "Intermediate_3": parent_log_lengths[1],
                "ratio_I3_to_I1": round(parent_log_lengths[1] / parent_log_lengths[0], 4),
                "interpretation": "Intermediate (1) and (3) are nearly equal on a logarithmic chart and have comparable duration.",
            },
            "Intermediate_1_internal": {
                "points": intermediate_1,
                "rules": impulse_rules(intermediate_1),
            },
            "Intermediate_3_internal": {
                "points": intermediate_3,
                "rules": impulse_rules(intermediate_3),
                "metrics": i3_metrics,
                "indicator_verdict": {
                    "normalized_ewo_divergence_5_vs_3": i3_metrics["Minor_5"]["ewo_peak_pct_abs"] < i3_metrics["Minor_3"]["ewo_peak_pct_abs"],
                    "rsi_divergence_5_vs_3": i3_metrics["Minor_5"]["rsi_max"] < i3_metrics["Minor_3"]["rsi_max"],
                    "volume_divergence_5_vs_3": i3_metrics["Minor_5"]["average_relative_volume_52"] < i3_metrics["Minor_3"]["average_relative_volume_52"],
                    "note": "Normalized EWO diverges. RSI and volume do not strictly diverge, so momentum evidence is supportive but mixed.",
                },
            },
        },
        "current_correction": {
            "preferred_structure": "Intermediate (4) ABC zigzag candidate",
            "points": {"start": 151.00, "A": 80.00, "B": 107.60, "C_low_candidate": live_low},
            "B_retracement_of_A_pct": round((107.60 - 80.00) / (151.00 - 80.00) * 100, 2),
            "C_to_A_ratio_so_far": round((107.60 - live_low) / (151.00 - 80.00), 3),
            "retracement_of_Intermediate_3_pct": round((151.00 - live_low) / (151.00 - 14.71) * 100, 2),
            "C_internal_points": c_subwaves,
            "C_internal_rules": impulse_rules(c_subwaves),
            "metrics": correction_metrics,
            "status": "The fifth wave of C extended to at least $74.84; no Intermediate (4) low is confirmed.",
        },
        "alternate": {
            "name": "Primary 1 ended at $99.58; Primary 2 ended at $56.13; Primary 3 active",
            "status": "Price-valid alternate, downgraded",
            "reason": "It requires a roughly ten-week Primary 2 after a roughly twenty-one-month Primary 1 and breaks the stronger logarithmic/time symmetry of the larger count.",
        },
        "decision_levels": {
            "low_candidate": live_low,
            "first_downside_extension": [63.72, 56.13],
            "bullish_evidence": "Five waves up from the final low, then three waves down that hold above it.",
            "first_reversal_levels": [82.52, 88.38, 107.60],
            "Intermediate_5_confirmation": 151.00,
            "hard_invalidation": "A sustained break below $33.34 overlaps Primary 1 Intermediate (1) and invalidates the standard impulse count.",
        },
        "verdict": {
            "hierarchy": "Primary 1 > Intermediate (4) active or forming a low > C wave possibly complete",
            "confidence": "Moderate-high for the completed Intermediate (3); low-moderate that $75.45 is the final Intermediate (4) low.",
            "stance": "No confirmed long entry yet; the rebound has not completed a five-up/three-down reversal test.",
        },
    }


def render_markdown(report):
    p = report["preferred_count"]
    i3 = p["Intermediate_3_internal"]
    c = report["current_correction"]
    return f"""# RKLB High-Degree Re-Audit

> **Alternate only:** The blind recount ending Primary 1 at $99.58 is the user-selected canonical RKLB hierarchy. This report is retained for comparison and must not overwrite the canonical database.

**Symbol:** NASDAQ:RKLB  
**Date:** 15 July 2026  
**Preferred hierarchy:** Primary 1 > Intermediate (4) active or forming a low

## High-Degree Count

| Wave | Price path | Status |
|---|---:|---|
| Intermediate (1) | $3.47 to $33.34 | Complete |
| Intermediate (2) | $33.34 to $14.71 | Complete |
| Intermediate (3) | $14.71 to $151.00 | Complete |
| Intermediate (4) | $151.00 to at least $74.84 | Active / low unconfirmed |
| Intermediate (5) | Future | Required to complete Primary 1 |

Intermediate (1) and (3) have logarithmic distances of **{p['parent_log_comparison']['Intermediate_1']}** and **{p['parent_log_comparison']['Intermediate_3']}**. Their ratio is **{p['parent_log_comparison']['ratio_I3_to_I1']}**, which is much stronger degree symmetry than the split count.

## Intermediate (3) Subdivision

`$14.71 -> $53.44 -> $38.26 -> $99.58 -> $56.13 -> $151.00`

This passes the mandatory Wave 3 length and Wave 4 no-overlap rules. Normalized EWO falls from **{i3['metrics']['Minor_3']['ewo_peak_pct_abs']}%** in Minor 3 to **{i3['metrics']['Minor_5']['ewo_peak_pct_abs']}%** in Minor 5. RSI and normalized volume do not strictly diverge, so the terminal evidence is mixed rather than perfect.

## Current Correction

The preferred Intermediate (4) structure is an ABC zigzag candidate:

- A: $151.00 to $80.00
- B: $80.00 to $107.60
- C: $107.60 to at least $74.84 and still unconfirmed

C subdivides as `$107.58 -> $97.91 -> $102.53 -> $80.51 -> $88.38 -> $74.84` and passes the two mandatory impulse rules. However, C is only **{c['C_to_A_ratio_so_far']} of A**, so completion requires reversal confirmation; extension toward **$63.72-$56.13** remains possible.

## Decision

- Bullish evidence: five waves up, followed by three waves down that hold above $74.84.
- Initial resistance: $82.52, $88.38, then $107.60.
- Intermediate (5) confirmation: sustained recovery through $151.00.
- Hard invalidation: sustained decline below $33.34.

The $99.58/$56.13 split remains a price-valid alternate, but it is downgraded because its proposed Primary 2 is disproportionately short in time and it loses the logarithmic symmetry of the larger count.
"""


def main():
    report = build_report()
    serialized = json.dumps(report, indent=2)
    (ROOT / "rklb_high_degree_reaudit_2026-07-15.json").write_text(serialized, encoding="utf-8")
    (ROOT / "rklb_elliott_wave_analysis.active_primary1_alternate.json").write_text(serialized, encoding="utf-8")
    (ROOT / "RKLB_High_Degree_Reaudit_Report_2026-07-15.md").write_text(render_markdown(report), encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
