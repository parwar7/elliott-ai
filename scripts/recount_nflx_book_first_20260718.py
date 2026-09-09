"""Fresh NFLX recount using raw TradingView OHLCV and classical Elliott rules only."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-07-18"


def number(value: str) -> float:
    return float(str(value).replace(",", "").replace("\u202f", ""))


def volume(value: str) -> float | None:
    text = str(value).replace("\u202f", "").strip()
    match = re.fullmatch(r"([0-9.]+)([KMB])?", text)
    if not match:
        return None
    scale = {None: 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2)]
    return float(match.group(1)) * scale


def load_rows(filename: str) -> pd.DataFrame:
    rows = json.loads((ROOT / filename).read_text(encoding="utf-8-sig"))
    parsed = []
    for row in rows:
        if row.get("date") == "-" or row.get("open") in (None, "∅"):
            continue
        date = None
        for fmt in ("%a %d %b '%y", "%a %d %b '%y %H:%M"):
            try:
                date = datetime.strptime(row["date"], fmt)
                break
            except ValueError:
                pass
        if date is None:
            continue
        parsed.append(
            {
                "date": date,
                **{field: number(row[field]) for field in ("open", "high", "low", "close")},
                "volume": volume(row.get("volume", "")),
            }
        )
    frame = pd.DataFrame(parsed).sort_values("date").reset_index(drop=True)
    median = (frame["high"] + frame["low"]) / 2
    frame["ewo"] = median.rolling(5).mean() - median.rolling(35).mean()
    frame["ewo_pct"] = frame["ewo"] / median * 100
    fast = frame["close"].ewm(span=12, adjust=False).mean()
    slow = frame["close"].ewm(span=26, adjust=False).mean()
    frame["macd"] = fast - slow
    frame["macd_pct"] = frame["macd"] / frame["close"] * 100
    return frame


def move(start: float, end: float) -> dict[str, float]:
    return {
        "points": round(abs(end - start), 4),
        "percent": round(abs(end / start - 1) * 100, 2),
        "log_return": round(abs(math.log(end / start)), 6),
    }


def retracement(origin: float, end_wave_1: float, end_wave_2: float) -> float:
    return round(abs(end_wave_1 - end_wave_2) / abs(end_wave_1 - origin) * 100, 2)


def validate_impulse(points: list[float]) -> dict[str, object]:
    if len(points) != 6:
        raise ValueError("An impulse requires six boundary prices")
    bullish = points[-1] > points[0]
    lengths = [
        move(points[0], points[1]),
        move(points[2], points[3]),
        move(points[4], points[5]),
    ]
    wave_2_holds = points[2] > points[0] if bullish else points[2] < points[0]
    wave_3_advances = points[3] > points[1] if bullish else points[3] < points[1]
    wave_4_holds = points[4] > points[2] if bullish else points[4] < points[2]
    no_overlap = points[4] > points[1] if bullish else points[4] < points[1]
    wave_3_not_shortest_log = lengths[1]["log_return"] >= min(
        lengths[0]["log_return"], lengths[2]["log_return"]
    )
    return {
        "valid": all((wave_2_holds, wave_3_advances, wave_4_holds, no_overlap, wave_3_not_shortest_log)),
        "wave_2_holds_origin": wave_2_holds,
        "wave_3_travels_beyond_wave_1": wave_3_advances,
        "wave_4_does_not_retrace_wave_3_fully": wave_4_holds,
        "wave_4_no_wave_1_overlap": no_overlap,
        "wave_3_not_shortest_by_log_return": wave_3_not_shortest_log,
        "actionary_lengths": lengths,
    }


def supporting_metrics(frame: pd.DataFrame, start: str, end: str) -> dict[str, float | None]:
    segment = frame[(frame["date"] >= start) & (frame["date"] <= end)]
    return {
        "bars": int(len(segment)),
        "average_volume": round(float(segment["volume"].dropna().mean()), 2) if segment["volume"].notna().any() else None,
        "max_abs_ewo_pct": round(float(segment["ewo_pct"].abs().max()), 4) if segment["ewo_pct"].notna().any() else None,
        "max_abs_macd_pct": round(float(segment["macd_pct"].abs().max()), 4) if segment["macd_pct"].notna().any() else None,
    }


def main() -> None:
    monthly = load_rows("tv_nflx_monthly.json")
    weekly = load_rows("tv_nflx_weekly_complete.json")
    daily = load_rows("tv_nflx_daily_fresh_20260718.json")
    two_hour = load_rows("tv_nflx_2h_fresh_20260718.json")

    cycle_i_prices = [0.0346, 0.5681, 0.1273, 4.35, 0.7544, 70.10]
    primary_v_prices = [0.7544, 6.54, 4.51, 42.32, 23.12, 70.10]
    cycle_iii_primary_1_prices = [16.27, 48.50, 35.21, 106.45, 85.39, 134.12]
    cycle_iii_p1_minor_prices = [16.27, 25.20, 21.17, 37.94, 28.53, 48.50]
    cycle_iii_p3_minor_prices = [35.21, 63.80, 55.22, 93.53, 83.44, 106.45]
    cycle_iii_p5_minor_prices = [85.39, 95.14, 90.67, 126.28, 118.06, 134.12]

    validations = {
        "cycle_i": validate_impulse(cycle_i_prices),
        "cycle_i_primary_v": validate_impulse(primary_v_prices),
        "cycle_iii_primary_1": validate_impulse(cycle_iii_primary_1_prices),
        "cycle_iii_primary_1_minor": validate_impulse(cycle_iii_p1_minor_prices),
        "cycle_iii_primary_3_minor": validate_impulse(cycle_iii_p3_minor_prices),
        "cycle_iii_primary_5_minor": validate_impulse(cycle_iii_p5_minor_prices),
    }
    assert all(check["valid"] for check in validations.values())

    cycle_ii_a = abs(70.10 - 35.15)
    cycle_ii_c = abs(45.85 - 16.27)
    current_w = abs(134.12 - 75.23)
    current_x = abs(108.95 - 75.23)

    report = {
        "symbol": "NASDAQ:NFLX",
        "as_of": AS_OF,
        "method": "Fresh book-first recount; previous NFLX reports and labels not read",
        "data": {
            "monthly": [str(monthly.date.min().date()), str(monthly.date.max().date()), len(monthly)],
            "weekly": [str(weekly.date.min().date()), str(weekly.date.max().date()), len(weekly)],
            "daily": [str(daily.date.min().date()), str(daily.date.max().date()), len(daily)],
            "two_hour": [str(two_hour.date.min()), str(two_hour.date.max()), len(two_hour)],
            "last_regular_session": {"low": 65.08, "close": 68.95, "date": "2026-07-17"},
            "origin_discrepancy": "Monthly adjusted history prints 0.0346; weekly adjusted history prints 0.0447. Monthly is used for the macro anchor and the discrepancy lowers exact-origin confidence.",
        },
        "preferred_hierarchy": {
            "cycle_i": {
                "path": [
                    ["I", "2002-10", 0.0346, "2004-01", 0.5681],
                    ["II", "2004-01", 0.5681, "2005-03", 0.1273],
                    ["III", "2005-03", 0.1273, "2011-07", 4.35],
                    ["IV", "2011-07", 4.35, "2012-08", 0.7544],
                    ["V", "2012-08", 0.7544, "2021-11", 70.10],
                ],
                "status": "completed impulse",
                "extension": "Primary V is extended at the Cycle-I scale, but contains a rule-valid five and therefore is not relabeled solely because fifth extensions are uncommon in stocks.",
            },
            "cycle_ii": {
                "path": [["A", 70.10, 35.15], ["B", 35.15, 45.85], ["C", 45.85, 16.27]],
                "status": "completed zigzag candidate (5-3-5)",
                "measurements": {
                    "total_retracement_pct": retracement(0.0346, 70.10, 16.27),
                    "c_to_a": round(cycle_ii_c / cycle_ii_a, 3),
                },
            },
            "cycle_iii": {
                "status": "active",
                "primary_1": [16.27, 48.50, 35.21, 106.45, 85.39, 134.12],
                "primary_2": {
                    "status": "active complex correction; W-X-Y preferred",
                    "W": [134.12, 75.23],
                    "X": [75.23, 108.95],
                    "Y": [108.95, 65.08],
                    "x_retracement_of_w_pct": round(current_x / current_w * 100, 2),
                    "y_to_w_so_far": round(abs(108.95 - 65.08) / current_w, 3),
                    "targets": {"W_equals_Y": 50.06, "Y_0.618_of_W": 72.55},
                },
            },
            "current_lower_degree": {
                "from_108.95": "A/first actionary leg to 70.86, corrective rebound to 78.44, then a declining motive candidate",
                "decline_from_78.44": [["i", 78.44, 72.52], ["ii", 72.52, 75.39], ["iii", 75.39, 65.08], ["iv", 65.08, 69.49]],
                "status": "Wave iv rebound candidate active; Wave v down remains possible",
                "invalidation": "A standard bearish impulse from 78.44 is invalidated if Wave iv enters Wave i territory above 72.52.",
                "near_targets": {"wave_v_equals_wave_i_from_69.49": 63.57, "wave_v_0.618_wave_i_from_69.49": 65.83},
            },
        },
        "validations": validations,
        "supporting_evidence": {
            "cycle_i_primary_i": supporting_metrics(weekly, "2002-10-01", "2004-01-31"),
            "cycle_i_primary_iii": supporting_metrics(weekly, "2005-03-01", "2011-07-31"),
            "cycle_i_primary_v": supporting_metrics(weekly, "2012-08-01", "2021-11-30"),
            "cycle_iii_primary_1": supporting_metrics(weekly, "2022-05-01", "2025-06-30"),
            "current_primary_2": supporting_metrics(weekly, "2025-06-23", "2026-07-18"),
        },
        "alternate": {
            "label": "Primary 2 as A-B-C zigzag from 134.12",
            "problem": "The 134.12-to-75.23 first leg is highly overlapping and has not been proven as a motive five on daily data. The book forbids calling a zigzag unless A is motive.",
            "promotion_condition": "Promote only if the first decline can be demonstrated as a valid impulse or leading diagonal and the decline from 108.95 completes a valid five as C.",
        },
        "decision_levels": {
            "72.52": "bearish micro-impulse overlap invalidation",
            "78.44": "break weakens the immediate continuation-down count",
            "108.95": "break invalidates the current W-X-Y pivot map",
            "50.06": "W=Y projection",
            "16.27": "hard invalidation that Cycle III began at the 2022 low",
        },
        "confidence": {
            "cycle_i_structure": "high",
            "cycle_ii_zigzag": "medium-high",
            "cycle_iii_primary_1": "high",
            "current_primary_2_family": "medium",
            "current_65.08_low_is_final": "low",
            "exact_degree_names": "medium",
        },
    }

    (ROOT / "nflx_book_first_recount_2026-07-18.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    md = f"""# NFLX Book-First Elliott Wave Recount

**Symbol:** NASDAQ:NFLX  
**As of:** {AS_OF}  
**Method:** Fresh recount from raw TradingView OHLCV only. No previous NFLX count was consulted.

## Preferred High-Degree Count

### Cycle I: $0.0346 to $70.10 (completed impulse)

| Primary | Path |
|---|---:|
| I | $0.0346 to $0.5681 |
| II | $0.5681 to $0.1273 |
| III | $0.1273 to $4.35 |
| IV | $4.35 to $0.7544 |
| V | $0.7544 to $70.10 |

All classical impulse rules pass on percentage/log measurement. Primary IV stays above the Primary-I high. Primary V is extended, but its internal count is a valid five: **$0.7544 -> $6.54 -> $4.51 -> $42.32 -> $23.12 -> $70.10**. The extension is therefore a guideline issue, not an invalidation.

### Cycle II: $70.10 to $16.27 (completed zigzag candidate)

**A:** $70.10 -> $35.15  
**B:** $35.15 -> $45.85  
**C:** $45.85 -> $16.27

C is **{cycle_ii_c / cycle_ii_a:.3f} x A**. This is proportionate but not a textbook Fibonacci equality, so structure carries more weight than the ratio. The total correction retraced **{retracement(0.0346, 70.10, 16.27):.2f}%** of Cycle I.

### Cycle III: active from $16.27

Primary 1 completed as:

**$16.27 -> $48.50 -> $35.21 -> $106.45 -> $85.39 -> $134.12**

This passes all hard impulse rules. Its internal Primary 1, 3 and 5 segments also contain rule-valid five-wave structures on weekly/daily data.

## Current Primary 2

The preferred correction is **W-X-Y**, not a forced A-B-C:

| Leg | Path |
|---|---:|
| W | $134.12 -> $75.23 |
| X | $75.23 -> $108.95 |
| Y | $108.95 -> $65.08, active |

X retraced **{current_x / current_w * 100:.2f}%** of W. Y has reached **{abs(108.95 - 65.08) / current_w:.3f} x W**. The 0.618 projection at **$72.55** has broken; equality is near **$50.06**.

The A-B-C alternate remains possible, but it is not preferred because the first decline from $134.12 to $75.23 has an overlapping corrective footprint and has not been proven as the motive five required for zigzag Wave A.

## Current Lower-Degree Position

From $78.44, the decline can be counted provisionally:

**i:** $78.44 -> $72.52  
**ii:** $72.52 -> $75.39  
**iii:** $75.39 -> $65.08  
**iv:** rebound toward $69.49, active candidate  
**v:** still possible

A Wave iv move above **$72.52** would overlap Wave i and invalidate this standard bearish impulse. If Wave iv ends near $69.49, Wave-v relationships point near **$65.83** and **$63.57**. The $65.08 low is therefore not confirmed as the end of Primary 2.

## Decision Levels

- **$72.52:** micro bearish-impulse invalidation.
- **$78.44:** weakens immediate continuation down.
- **$108.95:** invalidates the current W-X-Y pivot map.
- **$50.06:** W=Y projection.
- **$16.27:** hard invalidation of Cycle III from the 2022 low.

## Verdict

The strongest fresh count is **Cycle I complete at $70.10, Cycle II complete at $16.27, and Cycle III active**. Inside Cycle III, Primary 1 completed at $134.12 and Primary 2 is still active, preferably as a complex W-X-Y. Structural confidence is high through the 2025 high, but confidence that $65.08 is the final correction low is low.

Data note: the adjusted Monthly origin is $0.0346 while adjusted Weekly history prints $0.0447. This feed discrepancy affects the exact origin price, not the visible five-wave hierarchy.
"""
    (ROOT / "NFLX_Book_First_Recount_Report_2026-07-18.md").write_text(md, encoding="utf-8")
    print(json.dumps({"report": "NFLX_Book_First_Recount_Report_2026-07-18.md", "json": "nflx_book_first_recount_2026-07-18.json", "validations": validations}, indent=2))


if __name__ == "__main__":
    main()
