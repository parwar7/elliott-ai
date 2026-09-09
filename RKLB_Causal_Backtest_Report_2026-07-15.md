# RKLB Causal Elliott Wave Backtest

> **Superseded:** This targeted audit confirmed preselected pivots but did not establish their Elliott degrees. The blind recount in `RKLB_Blind_Recount_Report_2026-07-15.md` replaces its hierarchy.

**Symbol:** NASDAQ:RKLB  
**Test date:** 15 July 2026  
**Purpose:** Test the revised Primary 1 count without letting a pivot exist before later candles confirmed it.

## Method

- Daily pivots required five closed bars on the right before becoming available.
- The current two-hour pivot required four closed bars on the right.
- Previous labels were frozen; they were not silently rewritten after later price action.
- Hard Elliott rules were scored separately from supporting volume, EWO, and divergence evidence.
- This is a structural count test, not a trading-strategy performance test.

## Causal Checkpoints

| Proposed pivot | Pivot date | Available to the model | Result |
|---|---:|---:|---|
| Intermediate (1) high at $33.34 | 24 Jan 2025 | 31 Jan 2025 | Confirmed |
| Intermediate (2) low at $14.71 | 7 Apr 2025 | 14 Apr 2025 | Confirmed |
| Intermediate (3) Minor 1 high at $53.44 | 17 Jul 2025 | 24 Jul 2025 | Confirmed |
| Intermediate (3) Minor 2 low at $38.26 | 20 Aug 2025 | 27 Aug 2025 | Confirmed |
| Intermediate (3) Minor 3 high at $99.58 | 16 Jan 2026 | 26 Jan 2026 | Confirmed |
| Intermediate (3) Minor 4 low at $56.13 | 30 Mar 2026 | 7 Apr 2026 | Confirmed |
| Intermediate (3) Minor 5 high at $151.00 | 27 May 2026 | 3 Jun 2026 | Confirmed |
| Current lower-degree C low at $75.45 | 13 Jul 2026 22:00 | 14 Jul 2026 14:00 | Confirmed on 2h only |

**Causal pivot score:** 8/8.

## Structural Results

All three tested completed sequences passed the mandatory Wave 3 length and Wave 4 no-overlap rules:

1. Intermediate (1): passed.
2. Extended Intermediate (3): passed.
3. Current C-wave five-down candidate: passed.

**Mandatory sequence score:** 3/3.

For Intermediate (3), Minor 3 had stronger volume and EWO than Minor 1, while Minor 2 and Minor 4 had lower cumulative volume than the preceding motive waves. Wave 5 did not show strict combined volume/EWO divergence because EWO was stronger than in Wave 3.

The current five-wave C decline also passed Wave 3 strength, but Wave 5 did not produce strict combined volume/EWO divergence. This prevents the test from confirming that $75.45 is the final Intermediate (4) low.

## Causal Fibonacci Forecasts

These targets used only waves already confirmed at the time:

| Forecast | 1.618 target | Realized pivot | Error |
|---|---:|---:|---:|
| Minor 3 after Minor 2 confirmation | $100.93 | $99.58 | 1.33% |
| Minor 5 after Minor 4 confirmation | $155.35 | $151.00 | 2.80% |

## Verdict

- **Completed Intermediate (3): supported by the causal replay.**
- **Primary 1 active:** remains the preferred interpretation, but cannot be proven until Intermediate (5) forms.
- **Intermediate (4) low at $75.45:** unresolved. It is a confirmed two-hour local pivot, not a confirmed higher-degree reversal.
- **Primary 1 completed at $151:** remains a valid alternate and has not been disproven.
- **Hard invalidation:** a sustained break below $33.34 overlaps Intermediate (1) and invalidates the preferred standard Primary 1 impulse.

## Limitation

This was a targeted causal audit of the proposed RKLB count. Candidate pivots were specified before replay and then tested for causal confirmation. It does not prove that a fully automated Elliott Wave model would independently select the same pivots among every possible alternate count. Trading profitability also remains untested until entry, stop, sizing, cost, and exit rules are frozen.
