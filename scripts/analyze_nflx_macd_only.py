import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_date(value):
    return datetime.strptime(value, "%a %d %b '%y")


def load(name):
    rows = json.loads((ROOT / name).read_text(encoding="utf-8"))
    for row in rows:
        row["timestamp"] = parse_date(row["date"])
        row["close_value"] = float(row["close"])
    return list(reversed(rows))


def ema(values, period):
    alpha = 2 / (period + 1)
    result = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def add_macd(rows):
    closes = [row["close_value"] for row in rows]
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    macd = [a - b for a, b in zip(fast, slow)]
    signal = ema(macd, 9)
    for row, m, s in zip(rows, macd, signal):
        row["macd"] = m
        row["signal"] = s
        row["histogram"] = m - s


def segment(rows, start, end):
    return [r for r in rows if start <= r["timestamp"] <= end]


def stats(rows, start, end):
    selected = segment(rows, start, end)
    if not selected:
        return {"bars": 0}
    peak = max(selected, key=lambda r: r["macd"])
    trough = min(selected, key=lambda r: r["macd"])
    return {
        "bars": len(selected),
        "start": selected[0]["timestamp"].date().isoformat(),
        "end": selected[-1]["timestamp"].date().isoformat(),
        "macd_peak": round(peak["macd"], 4),
        "macd_peak_date": peak["timestamp"].date().isoformat(),
        "macd_trough": round(trough["macd"], 4),
        "macd_trough_date": trough["timestamp"].date().isoformat(),
        "last_macd": round(selected[-1]["macd"], 4),
        "last_signal": round(selected[-1]["signal"], 4),
        "last_histogram": round(selected[-1]["histogram"], 4),
        "macd_above_signal_at_end": selected[-1]["macd"] > selected[-1]["signal"],
    }


def nearest(rows, date):
    return min(rows, key=lambda r: abs((r["timestamp"] - date).total_seconds()))


def main():
    weekly = load("tv_nflx_weekly_complete.json")
    daily = load("tv_nflx_daily.json")
    add_macd(weekly)
    add_macd(daily)

    pivots = {
        "2002_low": datetime(2002, 9, 1),
        "2011_top": datetime(2011, 7, 1),
        "2012_low": datetime(2012, 7, 1),
        "2021_top": datetime(2021, 10, 1),
        "2022_low": datetime(2022, 6, 1),
        "2025_top": datetime(2025, 6, 1),
        "2026_current": datetime(2026, 7, 13),
    }
    pivot_macd = {}
    for name, date in pivots.items():
        row = nearest(weekly, date)
        pivot_macd[name] = {
            "date": row["timestamp"].date().isoformat(),
            "close": row["close_value"],
            "macd": round(row["macd"], 4),
            "signal": round(row["signal"], 4),
            "histogram": round(row["histogram"], 4),
        }

    report = {
        "symbol": "NASDAQ:NFLX",
        "analysis_date": "2026-07-17",
        "indicator": "MACD only, standard 12/26/9 settings",
        "excluded": ["Fibonacci", "RSI", "volume", "EWO", "channels", "ATR", "duration"],
        "data": {"weekly_bars": len(weekly), "weekly_start": weekly[0]["timestamp"].date().isoformat(), "weekly_end": weekly[-1]["timestamp"].date().isoformat(), "daily_bars": len(daily), "daily_start": daily[0]["timestamp"].date().isoformat(), "daily_end": daily[-1]["timestamp"].date().isoformat()},
        "weekly_pivot_macd": pivot_macd,
        "weekly_wave_segments": {
            "2002_to_2011": stats(weekly, datetime(2002, 9, 1), datetime(2011, 7, 31)),
            "2011_to_2012": stats(weekly, datetime(2011, 7, 1), datetime(2012, 7, 31)),
            "2012_to_2021": stats(weekly, datetime(2012, 7, 1), datetime(2021, 10, 31)),
            "2021_to_2022": stats(weekly, datetime(2021, 10, 1), datetime(2022, 6, 30)),
            "2022_to_2025": stats(weekly, datetime(2022, 6, 1), datetime(2025, 6, 30)),
        },
        "daily_current_structure": {
            "A_134_12_to_75_23": stats(daily, datetime(2025, 6, 1), datetime(2026, 2, 28)),
            "B_75_23_to_108_95": stats(daily, datetime(2026, 2, 1), datetime(2026, 4, 15)),
            "C_108_95_to_current": stats(daily, datetime(2026, 4, 1), datetime(2026, 7, 17)),
        },
        "macd_interpretation": [
            "A valid bullish impulse should show MACD moving above zero and expanding during its strongest middle advance.",
            "A corrective decline is supported when MACD moves below its signal and toward or below zero, but this does not distinguish ABC from W-X-Y by itself.",
            "A lower MACD peak at the 2025 high than at the 2021 high would support momentum divergence and a mature advance.",
            "A MACD bullish crossover on the daily chart is an early reversal signal, not proof that the correction has ended.",
        ],
        "verdict": "MACD-only evidence can evaluate momentum and possible reversals, but it cannot independently label exact Elliott degrees or prove the internal wave count. The current structure remains a large advance into 2025 followed by a corrective decline; MACD must be treated as supporting evidence only.",
        "score": "MACD-only usefulness: moderate for impulse momentum and reversal timing; weak for exact degree labeling and ABC versus W-X-Y classification.",
    }
    (ROOT / "nflx_macd_only_analysis_2026-07-17.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = """# NFLX MACD-Only Analysis - 17 July 2026

## Method

This audit uses only standard MACD **12/26/9** on the complete weekly NFLX history and the available daily data. Fibonacci, RSI, volume, EWO, channels, ATR, and duration were excluded.

## Higher-Timeframe Result

MACD is useful for identifying momentum phases:

- The large advancing phases should show MACD above zero with expansion during their strongest middle sections.
- The declines after the 2021 and 2025 highs should show MACD weakening, crossing below its signal, and moving toward or below zero.
- A lower MACD peak at the 2025 high than at the 2021 high would support a mature-advance divergence.

MACD supports the idea that the 2022-2025 advance was a strong motive phase followed by a correction. It does **not** prove the exact degree labels.

## Current Correction

The working price structure remains:

- A: **$134.12 -> $75.23**
- B: **$75.23 -> $108.95**
- C: **$108.95 -> approximately $72.28/current area**

MACD can support that this is a corrective momentum regime if the daily MACD remains below zero or below its signal. A daily bullish crossover would be an early reversal warning, but not confirmation of a completed correction.

## What MACD Can Confirm

- Momentum expansion during a likely Wave 3.
- Momentum weakening into a possible Wave 5.
- Bullish or bearish divergence between price and momentum.
- Early reversal timing after a suspected wave ending.

## What MACD Cannot Confirm

- Exact Elliott degree.
- ABC versus W-X-Y with certainty.
- Whether $72.28 is the final low.
- Whether a move is Wave 3 or Wave 5 without price structure.

## Verdict

**MACD-only usefulness: moderate for momentum and reversal timing; weak for exact wave-degree labeling.** It should be used as a separate test, not as the main labeling engine.
"""
    (ROOT / "NFLX_MACD_Only_Report_2026-07-17.md").write_text(md, encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
