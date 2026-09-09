from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_PATH = Path(r"C:\Users\Parwa\AppData\Local\Temp\elliott_top50_structural_scan_20260822.json")
DAILY_PATH = Path(r"C:\Users\Parwa\AppData\Local\Temp\elliott_top50_daily_20260822.json")
OUT_DIR = ROOT / "market_scans" / "2026-08-24_top50"

SYMBOLS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "AVGO", "GOOG", "META", "MU", "LLY",
    "TSLA", "JPM", "BRK.B", "AMD", "XOM", "JNJ", "V", "MA", "ABBV", "INTC",
    "WMT", "CSCO", "COST", "BAC", "PLTR", "AMAT", "LRCX", "CVX", "CAT", "MRK",
    "GE", "KO", "UNH", "NFLX", "HD", "PG", "PM", "GS", "RTX", "PANW",
    "GEV", "WFC", "MS", "KLAC", "TXN", "ORCL", "SNDK", "TMO", "AMGN", "LIN",
]

EXPOSURES = {
    "NVDA": "AI semis; real yields; AI capex", "AAPL": "devices; consumer; China; USD",
    "MSFT": "cloud/AI; capex; real yields", "AMZN": "AWS; retail; consumer; real yields",
    "GOOGL": "ads/cloud/AI; capex; regulation", "GOOG": "same Alphabet exposure as GOOGL",
    "AVGO": "AI/networking semis; capex", "META": "ads/AI; capex; real yields",
    "MU": "memory cycle; AI capex", "LLY": "drug pipeline; valuation; regulation",
    "TSLA": "EV demand; margins; rates; positioning", "JPM": "curve; credit; activity",
    "BRK.B": "insurance; financials; broad economy", "AMD": "AI/CPU semis; capex",
    "XOM": "oil; refining; geopolitics; USD", "JNJ": "healthcare; litigation; regulation",
    "V": "payments; consumer volume; credit", "MA": "payments; consumer volume; FX",
    "ABBV": "drug pipeline; regulation", "INTC": "semis; foundry capex; policy",
    "WMT": "consumer; food inflation; margins", "CSCO": "networking; enterprise capex",
    "COST": "consumer; valuation; food inflation", "BAC": "curve; credit; deposits",
    "PLTR": "software/AI; valuation; government", "AMAT": "wafer-fab capex; export policy",
    "LRCX": "memory/foundry capex; export policy", "CVX": "oil; refining; geopolitics; USD",
    "CAT": "global capex; commodities; USD", "MRK": "drug pipeline; regulation",
    "GE": "aerospace; industrial cycle", "KO": "defensive rotation; USD; input costs",
    "UNH": "medical costs; reimbursement; regulation", "NFLX": "subscriber/ad growth; valuation",
    "HD": "housing; mortgage rates; consumer", "PG": "defensive rotation; input costs; USD",
    "PM": "defensive yield; FX; regulation", "GS": "markets; deals; curve; credit",
    "RTX": "defense/aerospace; government budgets", "PANW": "cybersecurity; software duration",
    "GEV": "power/grid capex; electrification", "WFC": "curve; credit; deposits",
    "MS": "wealth/markets; curve; activity", "KLAC": "semiconductor capex; export policy",
    "TXN": "industrial/auto semis; cycle", "ORCL": "cloud/AI capex; software duration",
    "SNDK": "flash memory cycle; short listing history", "TMO": "biopharma tools; rates; funding",
    "AMGN": "drug pipeline; regulation", "LIN": "industrial activity; energy; USD",
}

STATE_TEXT = {
    "complex_correction_or_transition_candidate": "complex correction/transition candidate",
    "wave_2_or_4_correction_candidate": "Wave 2/4 pullback candidate",
    "wave_C_or_3_decline_candidate": "Wave C/3 decline candidate",
    "completed_up_impulse_candidate_then_correction": "up impulse candidate; correction active",
    "completed_down_impulse_candidate_then_correction": "down impulse candidate; rebound/transition",
    "wave_B_or_2_rebound_candidate": "Wave B/2 rebound candidate",
    "wave_3_or_5_continuation_candidate": "Wave 3/5 continuation candidate",
}

# Refined levels use daily plus available native 4-hour data. All remain conditional candidates.
OVERRIDES = {
    "AMZN": dict(bias="conditional long", trigger=266.40, invalidation=257.04,
                 targets=[282.79, 287.18, 308.11], note="3-leg pullback candidate; require 1h five up and retest",
                 evidence="D/W RSI 49.7/54.7; daily vol 0.74x; 4h RSI 43.6 and vol 0.58x support correction dry-up"),
    "PANW": dict(bias="conditional long", trigger=376.43, invalidation=343.01,
                 targets=[398.88, 410.60, 453.97], note="ABC-like pullback; Sep 1 earnings event risk",
                 evidence="D/W RSI 51.6/66.5; daily vol 0.91x; 4h RSI 45.3 and vol 0.67x support a pullback"),
    "META": dict(bias="conditional short", trigger=537.27, invalidation=567.00,
                 targets=[524.49, 479.80, 440.00], note="C/3 decline candidate; wait for break, rebound and failed reclaim",
                 evidence="D/W RSI 38.7/42.9; 4h RSI 38.6, but 4h vol 0.46x contradicts an expanding Wave 3"),
    "HD": dict(bias="conditional short", trigger=330.69, invalidation=350.40,
               targets=[315.31, 289.10, 272.69], note="post-earnings ABC/C candidate; housing-data sensitive",
               evidence="D/W RSI 46.0/48.0; 4h RSI 42.7 and vol 0.54x make a corrective decline plausible"),
    "GOOGL": dict(bias="two-sided", trigger=354.64, invalidation=338.57,
                  targets=[386.78, 408.61, 451.00], short_trigger=338.57, short_invalidation=354.64,
                  short_targets=[330.20, 314.90, 306.43], note="4h decline unproven; wait for range resolution",
                  evidence="D/W RSI 47.2/51.3; daily vol 0.78x; 4h RSI 47.3 and vol 0.46x give no motive confirmation"),
    "MSFT": dict(bias="conditional long", trigger=499.00, invalidation=477.15,
                 targets=[513.73, 537.63, 555.45], note="buy only a 499-505 reclaim/retest; candidate five-up is unproven",
                 evidence="D/W RSI 62.4/61.2; daily vol 0.61x; 4h RSI 54.1 and vol 0.62x are constructive, not proof"),
    "NFLX": dict(bias="two-sided", trigger=81.16, invalidation=76.80,
                 targets=[90.02, 92.54, 99.02], short_trigger=75.47, short_invalidation=81.16,
                 short_targets=[70.86, 65.08, 63.43], note="rebound motive candidate; 90.02 is the major discriminator",
                 evidence="D/W RSI 61.6/45.6; 4h RSI 61.9, but 4h vol 0.43x does not confirm an impulse"),
    "NVDA": dict(bias="two-sided watch", trigger=227.92, invalidation=214.50,
                 targets=[236.25, 254.76, 278.00], short_trigger=214.50, short_invalidation=227.92,
                 short_targets=[202.00, 190.02, 187.66], note="Friday selling volume expanded; no blind dip buy"),
    "TSLA": dict(bias="two-sided watch", trigger=366.50, invalidation=331.12,
                 targets=[413.54, 437.26, 445.14], short_trigger=331.12, short_invalidation=366.50,
                 short_targets=[297.38, 273.21, 260.36], note="B/2 rebound candidate; RSI alone cannot trigger a short"),
    "AAPL": dict(bias="range/no trade", trigger=320.28, invalidation=300.00,
                 targets=[344.57, 360.84, 382.00], short_trigger=300.00, short_invalidation=320.28,
                 short_targets=[289.05, 273.94, 259.44], note="complex range; no trade inside 300-320"),
    "BAC": dict(bias="bearish watch", trigger=65.22, invalidation=61.52,
                targets=[68.92, 72.62, 74.19], short_trigger=61.52, short_invalidation=65.22,
                short_targets=[59.20, 57.82, 54.12], note="4h selling volume expanded; daily pullback-long downgraded"),
    "RTX": dict(bias="watch only", trigger=226.41, invalidation=209.85,
                targets=[236.99, 243.00, 261.80], short_trigger=209.85, short_invalidation=226.41,
                short_targets=[193.29, 180.00, 170.78], note="decline looks too motive for a clean pullback long"),
    "JPM": dict(bias="two-sided watch", trigger=365.75, invalidation=350.37,
                targets=[381.13, 396.51, 411.41], short_trigger=350.37, short_invalidation=365.75,
                short_targets=[334.99, 319.61, 279.10], note="wait for lower-timeframe reversal or breakdown"),
    "WMT": dict(bias="watch only", trigger=114.56, invalidation=102.16,
                targets=[126.96, 139.36, 147.17], short_trigger=102.16, short_invalidation=114.56,
                short_targets=[89.76, 77.36, 71.85], note="oversold is not a long signal; selloff volume expanded"),
    "COST": dict(bias="range/no trade", trigger=978.70, invalidation=925.77,
                 targets=[1031.63, 1084.56, 1096.50], short_trigger=925.77, short_invalidation=978.70,
                 short_targets=[872.84, 819.91, 797.02], note="no clean edge inside 926-979"),
    "PM": dict(bias="range/no trade", trigger=194.26, invalidation=184.02,
               targets=[207.76, 214.74, 215.99], short_trigger=184.02, short_invalidation=194.26,
               short_targets=[173.78, 163.54, 136.07], note="complex correction; wait for range break and retest"),
    "MRK": dict(bias="extended/no chase", trigger=154.49, invalidation=144.90,
                targets=[164.08, 173.67, 181.00], short_trigger=144.90, short_invalidation=154.49,
                short_targets=[135.31, 125.72, 93.32], note="daily RSI 77.8; strength is real but entry quality is poor"),
}


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def generic_decision(symbol: str, item: dict) -> dict:
    state = item["context"]["elliott_state_candidate"]
    long = item["trade_candidates"]["long"]
    short = item["trade_candidates"]["short"]
    rsi = float(item["context"]["daily_rsi14"])
    if rsi >= 70:
        bias = "extended/no chase"
    elif state == "wave_C_or_3_decline_candidate":
        bias = "bearish watch"
    elif state == "wave_2_or_4_correction_candidate":
        bias = "pullback watch"
    elif state == "wave_3_or_5_continuation_candidate":
        bias = "continuation watch"
    else:
        bias = "watch only"
    return {
        "bias": bias,
        "trigger": float(long["trigger_above"]),
        "invalidation": float(long["invalidation_below"]),
        "targets": [float(long["conditional_target"])],
        "short_trigger": float(short["trigger_below"]),
        "short_invalidation": float(short["invalidation_above"]),
        "short_targets": [float(short["conditional_target"])],
        "note": "daily screen only; lower-timeframe confirmation required",
    }


def recent_pivots(item: dict) -> list[dict]:
    pivots = item["daily_pivots"][-6:]
    result: list[dict] = []
    for pivot in pivots:
        result.append({
            "date": pivot["date"],
            "price": float(pivot["price"]),
            "type": pivot["type"],
            "confirmed_pivot": bool(pivot["confirmed"]),
        })
    return result


def fibonacci_geometry(item: dict) -> dict:
    confirmed = [p for p in item["daily_pivots"] if p["confirmed"]]
    end = confirmed[-1]
    start = next(
        p for p in reversed(confirmed[:-1])
        if p["type"] != end["type"] and p["date"] != end["date"]
    )
    start_price = float(start["price"])
    end_price = float(end["price"])
    magnitude = abs(end_price - start_price)
    up = start["type"] == "L" and end["type"] == "H"
    if up:
        retracement = {key: end_price - ratio * magnitude for key, ratio in (("0.382", 0.382), ("0.500", 0.5), ("0.618", 0.618))}
        extension = {key: end_price + ratio * magnitude for key, ratio in (("0.618", 0.618), ("1.000", 1.0), ("1.618", 1.618))}
        direction = "up-leg pullback support"
    else:
        retracement = {key: end_price + ratio * magnitude for key, ratio in (("0.382", 0.382), ("0.500", 0.5), ("0.618", 0.618))}
        extension = {key: end_price - ratio * magnitude for key, ratio in (("0.618", 0.618), ("1.000", 1.0), ("1.618", 1.618))}
        direction = "down-leg rebound resistance"
    return {
        "basis": "last two opposite-type confirmed daily screening pivots",
        "direction": direction,
        "start": {"date": start["date"], "price": start_price, "type": start["type"]},
        "end": {"date": end["date"], "price": end_price, "type": end["type"]},
        "retracement": retracement,
        "extension": extension,
    }


def pivot_labels(state: str, count: int) -> list[str]:
    patterns = {
        "wave_2_or_4_correction_candidate": ["3/5?", "A?", "B?", "C?", "X?", "Y?"],
        "wave_C_or_3_decline_candidate": ["A/1?", "B/2?", "C/3?", "4?", "5?", "Alt?"],
        "completed_up_impulse_candidate_then_correction": ["2?", "3?", "4?", "5?", "A?", "B?"],
        "completed_down_impulse_candidate_then_correction": ["2?", "3?", "4?", "5?", "A/1?", "B/2?"],
        "wave_B_or_2_rebound_candidate": ["W/A?", "X/B?", "Y/C?", "A/1?", "B/2?", "C/3?"],
        "wave_3_or_5_continuation_candidate": ["1/3?", "2/4?", "3/5?", "4?", "5?", "Ext?"],
        "complex_correction_or_transition_candidate": ["W/A?", "X/B?", "Y/C?", "X2?", "Z?", "Alt?"],
    }
    values = patterns.get(state, ["P1?", "P2?", "P3?", "P4?", "P5?", "P6?"])
    return values[-count:]


def build_records(scan: dict) -> list[dict]:
    rows: list[dict] = []
    for symbol in SYMBOLS:
        item = scan["symbols"][symbol]
        context = item["context"]
        decision = generic_decision(symbol, item)
        decision.update(OVERRIDES.get(symbol, {}))
        decision["two_r_gate"] = float(decision["trigger"]) + 2 * (
            float(decision["trigger"]) - float(decision["invalidation"])
        )
        if "short_trigger" in decision:
            decision["short_two_r_gate"] = float(decision["short_trigger"]) - 2 * (
                float(decision["short_invalidation"]) - float(decision["short_trigger"])
            )
        pivots = recent_pivots(item)
        labels = pivot_labels(context["elliott_state_candidate"], len(pivots))
        for pivot, label in zip(pivots, labels):
            pivot["candidate_label"] = label
        rows.append({
            "symbol": symbol,
            "exchange": item["metadata"]["exchange"],
            "close_2026_08_21": float(context["close"]),
            "daily_rsi14": float(context["daily_rsi14"]),
            "weekly_rsi14": float(context["weekly_rsi14"]),
            "volume_ratio_20d": float(context["volume_ratio20"]),
            "distance_from_52w_high_pct": float(context["distance_from_high_pct"]),
            "state_code": context["elliott_state_candidate"],
            "where_now": STATE_TEXT[context["elliott_state_candidate"]],
            "exposure": EXPOSURES[symbol],
            "decision": decision,
            "candidate_fibonacci": fibonacci_geometry(item),
            "recent_daily_candidate_pivots": pivots,
            "history_limit": (
                "2020-09-30" if symbol == "PLTR" else
                "2024-04-01" if symbol == "GEV" else
                "2025-02-13" if symbol == "SNDK" else "2014-09-17"
            ),
        })
    return rows


def fmt(value: float) -> str:
    if value < 2:
        return f"{value:.5f}"
    return f"{value:,.2f}"


def report_markdown(records: list[dict], artifact_hash: str) -> str:
    ranked = ["AMZN", "PANW", "META", "HD", "GOOGL", "MSFT", "NFLX"]
    by_symbol = {r["symbol"]: r for r in records}
    lines = [
        "# Top 50 US Stocks and FX: Monday 24 August 2026",
        "",
        "**Decision-time market data:** Friday 21 August 2026 close. **Status:** price-first screening, not verified parent-wave proof.",
        "",
        "## Executive Decision",
        "",
        "Do not place a market order at Monday's open. The opening gap is unknown, most mega-cap exposures are correlated, and the decisive US data arrive later in the week. Wait for a completed 30-minute or 1-hour break/retest, preserve the stated invalidation, and take nothing that cannot offer approximately 2R to a realistic target.",
        "",
        "The strongest conditional candidates are AMZN and PANW long, META and HD short, with GOOGL, MSFT and NFLX requiring a two-step confirmation. These are hypotheses, not guaranteed trades.",
        "",
        "## Market Gate",
        "",
        "- SPY closed 765.72 with daily/4h RSI about 54.2/48.3. A break below 762.04, then 755.58, weakens every long setup; a break/retest above 779.37 supports them.",
        "- QQQ closed 713.44 with daily/4h RSI about 50.1/46.3. Below 708.52 is a technology-risk warning; above 734.58 is the cleaner continuation confirmation.",
        "- A gap through a trigger is not an entry. Require a retest or a newly completed lower-timeframe structure and recalculate reward/risk from the actual price.",
        "",
        "## Ranked Conditional Setups",
        "",
        "| Rank | Symbol | Candidate | Confirmation | Invalidation | Approx. 2R gate | Conditional targets | RSI/volume evidence | Why / warning |",
        "|---:|---|---|---:|---:|---|---|---|---|",
    ]
    for rank, symbol in enumerate(ranked, 1):
        row = by_symbol[symbol]
        d = row["decision"]
        targets = " / ".join(fmt(v) for v in d["targets"])
        trigger = fmt(d["trigger"])
        invalidation = fmt(d["invalidation"])
        two_r = fmt(d["two_r_gate"])
        if d["bias"] == "two-sided":
            trigger += f" long; {fmt(d['short_trigger'])} short"
            invalidation += f" long; {fmt(d['short_invalidation'])} short"
            two_r += f" long; {fmt(d['short_two_r_gate'])} short"
            targets += "; short " + " / ".join(fmt(v) for v in d["short_targets"])
        lines.append(
            f"| {rank} | {symbol} | {d['bias']} | {trigger} + retest | {invalidation} | {two_r} | {targets} | {d.get('evidence', 'See full table')} | {d['note']} |"
        )
    lines += [
        "",
        "## FX and Dollar",
        "",
        "| Market | Candidate state | Confirmation | Invalidation | Candidate targets | Evidence limitation |",
        "|---|---|---|---|---|---|",
        "| DXY | decline from 101.6 area is A/1 or C/3 candidate | bearish below 98.55; bullish reclaim 100.00 | opposite side of break | down 97.80 / 96.90 / 95.60; up 101.60 | ICEUS TradingView price only; index volume and exact RSI unavailable |",
        "| EUR/USD | 4h bearish-divergence candidate inside strong daily advance | above 1.17110 long or below 1.16694 short, then retest | 1.16694 long / 1.17110 short | 1.17950 long; 1.15860 short | FX volume unavailable; Friday bars only |",
        "| GBP/USD | extended daily advance; reversal not confirmed | above 1.36752 long or below 1.36188 short, then retest | 1.36188 long / 1.36752 short | 1.37880 long; 1.35060 short | FX volume unavailable; Friday bars only |",
        "| USD/GBP | exact reciprocal view of GBP/USD | use inverse GBP/USD levels | same relationship | approximately 0.7313 upside trigger when GBP breaks lower | not an independent signal |",
        "",
        "## Complete Top-50 Screen",
        "",
        "All Elliott labels below are candidate states. The screener validates price rules where possible, but the daily/weekly pivot scan is not a recursive lower-timeframe proof engine.",
        "",
        "| Symbol | Close | D RSI | W RSI | Vol/20d | Candidate position | Monday status | Main decision level | Fib 38/50/61.8 | Exposure |",
        "|---|---:|---:|---:|---:|---|---|---:|---|---|",
    ]
    for row in records:
        d = row["decision"]
        decision_level = d.get("short_trigger") if d["bias"] in {"conditional short", "bearish watch"} else d["trigger"]
        fib = row["candidate_fibonacci"]["retracement"]
        fib_zone = "/".join(fmt(fib[key]) for key in ("0.382", "0.500", "0.618"))
        lines.append(
            f"| {row['symbol']} | {fmt(row['close_2026_08_21'])} | {row['daily_rsi14']:.1f} | "
            f"{row['weekly_rsi14']:.1f} | {row['volume_ratio_20d']:.2f} | {row['where_now']} | "
            f"{d['bias']} | {fmt(decision_level)} | {fib_zone} | {row['exposure']} |"
        )
    lines += [
        "",
        "## Macro and Fundamental Scenario Layer",
        "",
        "### Confirmed facts at the cutoff",
        "",
        "- EFFR and SOFR were both 3.63% on 20 August; the federal-funds target range was 3.50%-3.75%.",
        "- The 2-year Treasury yield was 4.19%, the 10-year 4.69%, and 10-year real yield 2.35% on 20 August.",
        "- High-yield OAS was 2.75% and VIX 16.01: risk pricing was not distressed.",
        "- Reserve balances were about $2.94tn on 19 August, Treasury's general account about $954bn, and ON RRP about $0.2bn on 21 August. Near-zero RRP leaves less idle-liquidity buffer; this is an interpretation, not a timing signal.",
        "- The Fed held rates on 29 July. Next week's important scheduled events are new-home sales Tuesday, GDP/PCE/durables Wednesday, and the Fed Chair's Jackson Hole speech Friday.",
        "",
        "### Competing narratives",
        "",
        "1. **Rates-ease / correction-completes:** softer activity or inflation data lowers real yields and DXY. That would support AMZN, MSFT, GOOGL and semiconductors after technical confirmation.",
        "2. **Rates-stay-high / valuation compression:** firm GDP, PCE or a hawkish Jackson Hole message lifts real yields and the dollar. META/HD downside and weak-tech corrections could extend without any new company-specific bad news.",
        "3. **Rotation rather than index trend:** credit spreads and VIX remain calm while leadership rotates toward healthcare, defense, energy or staples. In that case SPY may hold while individual Elliott counts diverge sharply.",
        "",
        "### Verified company-event context for the shortlist",
        "",
        "Amazon, Meta and Microsoft had already reported in late July, while Home Depot reported on 18 August. Palo Alto Networks has a scheduled earnings call on 1 September, so a PANW setup held beyond this week carries defined event risk. These facts may support or contradict a scenario but did not alter the technical counts.",
        "",
        "### Adversarial review",
        "",
        "The strongest argument against the long setups is expanded selling in NVDA/BAC, QQQ below its recent high and still-high real yields. The strongest argument against the shorts is that credit spreads and VIX remain benign, several declines have contracted volume, and a softer macro print could turn them into completed corrections quickly.",
        "",
        "## Data, Method and Limits",
        "",
        "- Universe: the 50 largest SPY holdings by weight in State Street's 20 August 2026 holdings file. GOOG and GOOGL are one company exposure and must not be double-counted.",
        "- Technical feed: Twelve Data, regular session, split-adjusted and dividend-unadjusted OHLCV, through 21 August 2026. Daily history is 3,000 rows for 48 names; PLTR, GEV and SNDK have shorter histories.",
        "- Native 4-hour refinement was run only for the shortlisted equities and SPY/QQQ. Intraday FX volume is unavailable/incomparable. DXY is from the visible ICEUS TradingView chart because Twelve Data did not resolve the index symbol.",
        "- Price structure was screened first. Fibonacci, RSI and same-feed volume are supporting or contradictory evidence only; none can verify a wave or invalidate a structurally valid alternate by itself.",
        "- Same-bar high/low pivots and every unproved subdivision remain candidates. The overlay marks these with `?` and draws no wave-path lines.",
        "",
        "## Official Sources",
        "",
        "- [State Street SPY holdings](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy)",
        "- [New York Fed reference rates](https://www.newyorkfed.org/markets/reference-rates)",
        "- [Federal Reserve July 2026 decision](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm) and [August calendar](https://www.federalreserve.gov/newsevents/2026-august.htm)",
        "- [BEA release schedule](https://www.bea.gov/news/schedule/full) and [Census economic-indicator calendar](https://www.census.gov/economic-indicators/calendar-listview.html)",
        "- [FRED Treasury yields](https://fred.stlouisfed.org/series/DGS10), [reserve balances](https://fred.stlouisfed.org/series/WRESBAL), [Treasury General Account](https://fred.stlouisfed.org/series/WTREGEN), and [ON RRP](https://fred.stlouisfed.org/series/RRPONTSYD)",
        "- [ECB July policy decision](https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.mp260723~29f24d99bc.en.html) and [Bank of England July decision](https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/july-2026)",
        "- [Amazon Q2 results](https://ir.aboutamazon.com/news-release/news-release-details/2026/Amazon-com-Announces-Second-Quarter-Results/default.aspx), [Meta Q2 results](https://investor.atmeta.com/investor-news/press-release-details/2026/Meta-Reports-Second-Quarter-2026-Results/default.aspx), [Home Depot events](https://ir.homedepot.com/events-and-presentations), and [Palo Alto Networks events](https://investors.paloaltonetworks.com/news-and-events/events-presentations)",
        "",
        f"**Machine artifact hash:** `{artifact_hash}`",
    ]
    return "\n".join(lines) + "\n"


def pine_script(records: list[dict], artifact_hash: str) -> str:
    out = [
        "//@version=6",
        'indicator("Top 50 US + FX Elliott Decision Map 2026-08-24", overlay=true, max_lines_count=40, max_labels_count=120)',
        "",
        'showPivots = input.bool(true, "Candidate pivots")',
        'showLevels = input.bool(true, "Decision and Fibonacci levels")',
        'showPanel = input.bool(false, "Evidence panel")',
        'showBothSides = input.bool(false, "Show both directional triggers")',
        "",
        "candidateColor = color.rgb(126, 34, 206)",
        "longColor = color.rgb(21, 128, 61)",
        "shortColor = color.rgb(185, 28, 28)",
        "targetColor = color.rgb(37, 99, 235)",
        "fibColor = color.rgb(217, 119, 6)",
        "mutedColor = color.rgb(82, 82, 91)",
        "",
        "var labels = array.new<label>()",
        "var lines = array.new<line>()",
        "var table panel = table.new(position.top_right, 2, 7, border_width=1)",
        "",
        "clearObjects() =>",
        "    while array.size(labels) > 0",
        "        label.delete(array.pop(labels))",
        "    while array.size(lines) > 0",
        "        line.delete(array.pop(lines))",
        "",
        "addPivot(int when, float price, string labelText, bool above, int stack) =>",
        "    float spacing = math.max(price * 0.012, syminfo.mintick * 80)",
        "    float y = price + (above ? 1 : -1) * spacing * (stack + 1)",
        "    label id = label.new(when, y, labelText, xloc=xloc.bar_time, yloc=yloc.price, style=label.style_none, textcolor=candidateColor, size=size.small)",
        "    array.push(labels, id)",
        "",
        "addLevel(float price, string labelText, color c, string style) =>",
        "    line id = line.new(bar_index - 80, price, bar_index + 30, price, xloc=xloc.bar_index, extend=extend.right, color=color.new(c, 15), style=style, width=1)",
        "    array.push(lines, id)",
        "    label lab = label.new(bar_index + 2, price, labelText, xloc=xloc.bar_index, yloc=yloc.price, style=label.style_none, textcolor=c, size=size.tiny)",
        "    array.push(labels, lab)",
        "",
        "setPanel(string status, float drsi, float wrsi, float vr, string note, string source) =>",
        "    table.clear(panel, 0, 0, 1, 6)",
        "    table.cell(panel, 0, 0, syminfo.ticker, text_color=color.white, bgcolor=mutedColor)",
        "    table.cell(panel, 1, 0, status, text_color=color.white, bgcolor=mutedColor)",
        '    table.cell(panel, 0, 1, "Daily RSI")',
        '    table.cell(panel, 1, 1, na(drsi) ? "Unavailable" : str.tostring(drsi, "#.0"))',
        '    table.cell(panel, 0, 2, "Weekly RSI")',
        '    table.cell(panel, 1, 2, na(wrsi) ? "Unavailable" : str.tostring(wrsi, "#.0"))',
        '    table.cell(panel, 0, 3, "Volume / 20d")',
        '    table.cell(panel, 1, 3, na(vr) ? "Unavailable" : str.tostring(vr, "#.00"))',
        '    table.cell(panel, 0, 4, "Status")',
        "    table.cell(panel, 1, 4, note)",
        '    table.cell(panel, 0, 5, "Source")',
        "    table.cell(panel, 1, 5, source)",
        '    table.cell(panel, 0, 6, "Proof")',
        '    table.cell(panel, 1, 6, "Candidate / unproven")',
        "",
    ]
    dispatch: list[tuple[str, str]] = []
    for row in records:
        d = row["decision"]
        function_name = "render_" + row["symbol"].replace(".", "_").replace("-", "_")
        dispatch.append((row["symbol"], function_name))
        primary_short = d["bias"] in {"conditional short", "bearish watch"}
        primary_trigger = float(d.get("short_trigger", d["trigger"])) if primary_short and d["bias"] == "bearish watch" else float(d["trigger"])
        primary_invalidation = float(d.get("short_invalidation", d["invalidation"])) if primary_short and d["bias"] == "bearish watch" else float(d["invalidation"])
        primary_targets = d.get("short_targets", d["targets"]) if primary_short and d["bias"] == "bearish watch" else d["targets"]
        primary_word = "Short" if primary_short else "Long"
        primary_color = "shortColor" if primary_short else "longColor"
        out += [
            f"{function_name}() =>",
            f'    if showPanel\n        setPanel("{d["bias"]}", {row["daily_rsi14"]:.6f}, {row["weekly_rsi14"]:.6f}, {row["volume_ratio_20d"]:.6f}, "{row["where_now"]}", "Twelve Data D/W; 4h where stated")',
        ]
        pivots = row["recent_daily_candidate_pivots"]
        date_stacks: dict[str, int] = {}
        for pivot in pivots:
            y, m, day = [int(x) for x in pivot["date"].split("-")]
            stack = date_stacks.get(pivot["date"], 0)
            date_stacks[pivot["date"]] = stack + 1
            above = "true" if pivot["type"] == "H" else "false"
            label = pivot["candidate_label"]
            out.append(
                f'    if showPivots\n        addPivot(timestamp("America/New_York", {y}, {m}, {day}, 16, 0), {pivot["price"]:.8f}, "{label}", {above}, {stack})'
            )
        out += [
            f'    if showLevels\n        addLevel({primary_trigger:.8f}, "{primary_word} confirm {fmt(primary_trigger)}", {primary_color}, line.style_solid)',
            f'    if showLevels\n        addLevel({primary_invalidation:.8f}, "{primary_word} invalid {fmt(primary_invalidation)}", fibColor, line.style_dashed)',
        ]
        for index, target in enumerate(primary_targets[:3], 1):
            out.append(f'    if showLevels\n        addLevel({float(target):.8f}, "{primary_word} T{index} {fmt(float(target))}", targetColor, line.style_dotted)')
        fib = row["candidate_fibonacci"]["retracement"]
        for key in ("0.382", "0.500", "0.618"):
            out.append(f'    if showLevels\n        addLevel({float(fib[key]):.8f}, "Fib {key} {fmt(float(fib[key]))}", fibColor, line.style_dotted)')
        if "short_trigger" in d and not primary_short:
            out += [
                f'    if showLevels and showBothSides\n        addLevel({float(d["short_trigger"]):.8f}, "Short confirm {fmt(float(d["short_trigger"]))}", shortColor, line.style_solid)',
                f'    if showLevels and showBothSides\n        addLevel({float(d["short_invalidation"]):.8f}, "Short invalid {fmt(float(d["short_invalidation"]))}", fibColor, line.style_dashed)',
            ]
            for index, target in enumerate(d["short_targets"][:3], 1):
                out.append(f'    if showLevels and showBothSides\n        addLevel({float(target):.8f}, "Short T{index} {fmt(float(target))}", targetColor, line.style_dotted)')
        out += ["    true", ""]

    fx_blocks = [
        ("DXY", "A/1 or C/3 decline?", None, None, None, 100.00, 98.55, [101.60, 102.40], 98.55, 100.00, [97.80, 96.90, 95.60], "ICEUS price; RSI/volume unavailable"),
        ("EURUSD", "advance; 4h divergence?", 75.6, None, None, 1.17110, 1.16694, [1.17950, 1.18490], 1.16694, 1.17110, [1.15860, 1.15670, 1.15120], "Twelve Data; FX volume unavailable"),
        ("GBPUSD", "extended advance?", 75.5, None, None, 1.36752, 1.36188, [1.37880, 1.39000], 1.36188, 1.36752, [1.35060, 1.34700], "Twelve Data; FX volume unavailable"),
        ("USDGBP", "inverse GBP/USD", None, None, None, 0.73428, 0.73125, [0.74041, 0.74400], 0.73125, 0.73428, [0.72530, 0.71942], "Reciprocal view; not independent"),
    ]
    for sym, status, drsi, wrsi, vr, long_t, long_i, long_targets, short_t, short_i, short_targets, source in fx_blocks:
        function_name = "render_" + sym.replace(".", "_").replace("-", "_")
        dispatch.append((sym, function_name))
        dval = "na" if drsi is None else f"{drsi:.3f}"
        wval = "na" if wrsi is None else f"{wrsi:.3f}"
        vval = "na" if vr is None else f"{vr:.3f}"
        out += [
            f"{function_name}() =>",
            f'    if showPanel\n        setPanel("two-sided", {dval}, {wval}, {vval}, "{status}", "{source}")',
            f'    if showLevels\n        addLevel({long_t:.8f}, "Long confirm {fmt(long_t)}", longColor, line.style_solid)',
            f'    if showLevels\n        addLevel({long_i:.8f}, "Long invalid {fmt(long_i)}", fibColor, line.style_dashed)',
        ]
        for i, target in enumerate(long_targets, 1):
            out.append(f'    if showLevels\n        addLevel({target:.8f}, "Long T{i} {fmt(target)}", targetColor, line.style_dotted)')
        out += [
            f'    if showLevels and showBothSides\n        addLevel({short_t:.8f}, "Short confirm {fmt(short_t)}", shortColor, line.style_solid)',
            f'    if showLevels and showBothSides\n        addLevel({short_i:.8f}, "Short invalid {fmt(short_i)}", fibColor, line.style_dashed)',
        ]
        for i, target in enumerate(short_targets, 1):
            out.append(f'    if showLevels and showBothSides\n        addLevel({target:.8f}, "Short T{i} {fmt(target)}", targetColor, line.style_dotted)')
        out += ["    true", ""]
    out += [
        "if barstate.islast",
        "    clearObjects()",
        "    string sym = syminfo.ticker",
        "    bool supported = switch sym",
    ]
    out += [f'        "{symbol}" => {function_name}()' for symbol, function_name in dispatch]
    out += [
        "        => false",
        "    if showPanel and not supported",
        "        table.clear(panel, 0, 0, 1, 6)",
        '        table.cell(panel, 0, 0, "Unsupported symbol")',
        '        table.cell(panel, 1, 0, "Open one of the report symbols")',
        "",
        f"// Artifact lineage: {artifact_hash}",
        "// All wave labels are candidate-only and deliberately carry question marks.",
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    scan = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    daily = json.loads(DAILY_PATH.read_text(encoding="utf-8"))
    records = build_records(scan)
    payload = {
        "schema_version": "top50-monday-conditional-scan-1.0.0",
        "created_at_utc": "2026-08-22T08:15:00Z",
        "market_cutoff": "2026-08-21T20:00:00Z",
        "universe": "Top 50 SPY holdings by weight as of 2026-08-20",
        "provider": "Twelve Data",
        "price_basis": "split-adjusted, dividend-unadjusted",
        "session": "regular",
        "source_daily_hash": daily["content_hash"],
        "source_scan_hash": scan["content_hash"],
        "method": "price-first daily/weekly screen plus selective native-4h refinement; candidate-only",
        "records": records,
        "cross_assets": {
            "DXY": {"source": "visible TradingView ICEUS daily chart", "close": 98.839, "rsi": "unavailable", "volume": "unavailable"},
            "EURUSD": {"source": "Twelve Data", "close": 1.16764, "daily_rsi14": 75.6, "four_hour_rsi14": 61.6, "volume": "unavailable"},
            "GBPUSD": {"source": "Twelve Data", "close": 1.36453, "daily_rsi14": 75.5, "four_hour_rsi14": 67.9, "volume": "unavailable"},
            "USDGBP": {"source": "reciprocal of GBPUSD", "close": round(1 / 1.36453, 8), "independent_signal": False},
        },
        "warnings": [
            "No wave is recursively verified by this broad screen.",
            "Opening gaps are unknown; no market-on-open entry is authorized by the analysis.",
            "GOOG and GOOGL are the same company exposure.",
            "Intraday FX volume is unavailable and DXY volume/RSI were not evaluated.",
        ],
    }
    payload["content_hash"] = canonical_hash(payload)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "top50_us_fx_scan_20260821.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    (OUT_DIR / "TOP50_US_FX_ELLIOTT_SCAN_2026-08-24.md").write_text(
        report_markdown(records, payload["content_hash"]), encoding="ascii"
    )
    (OUT_DIR / "TOP50_US_FX_DECISION_OVERLAY_2026-08-24.pine").write_text(
        pine_script(records, payload["content_hash"]), encoding="ascii"
    )
    print(json.dumps({
        "output_directory": str(OUT_DIR),
        "record_count": len(records),
        "content_hash": payload["content_hash"],
    }, indent=2))


if __name__ == "__main__":
    main()
