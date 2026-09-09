# Current AI Brain

Source of truth: `AI_BRAIN_MASTER_RULES.json`

Deprecated source: the earlier YouTube video notes are no longer active and should not be used for future chart analysis in this task.

Latest integrated learning source:

- `https://www.investmenttheory.org/uploads/3/4/8/2/34825752/elliott-wave-principle.pdf`
- Title: `Elliott Wave Principle / Comprehensive Course` by A.J. Frost and Robert Prechter
- Status: integrated as the primary classical reference on `2026-07-18`.

Earlier supporting source:

- `https://youtu.be/TxpTOT5AQwM`
- Title: `What's the SECRET to Accurately Labeling ELLIOTT WAVE Charts?`
- Status: integrated into the master rules on `2026-07-13`.

## Active Method

The AI brain now follows the attached master execution pipeline:

1. Start from the highest available timeframe, preferably Monthly or 3-Month.
2. Locate the absolute historical origin point, called Point Zero.
3. Build the largest parent wave structure first.
4. Drill down recursively using the Russian Doll Constraint:
   each lower-timeframe wave must stay inside its parent wave start/end date and price boundaries.
5. Validate every wave by its internal subwave structure before accepting it.
6. Apply classical price rules and required internal structure first. Use MACD/EWO, volume, Fibonacci, channels, personality, and multi-timeframe agreement only to rank otherwise valid counts.
7. Inspect both arithmetic and semilogarithmic charts. Use percentage relationships for waves above Intermediate degree and whichever scale produces the more coherent channel; never select a scale only because it favors a preferred count.
8. For live markets, track the active unfinished wave with 1H and 15m data after the macro structure is locked.
9. Run the sequence volume verifier only when Waves 1-5 share a comparable volume metric and corporate-action-adjusted price/volume basis.

## Blind Recount Default

All future chart analysis must use the user-selected RKLB blind-recount workflow before promoting a count to canonical:

1. Detect confirmed price pivots without looking at proposed Elliott labels or indicators.
2. Identify complete motive-correction-motive blocks before assigning degrees.
3. Generate and compare at least two high-degree counts whenever the hierarchy is ambiguous.
4. Apply mandatory Elliott price rules first, then prove internal subdivisions on lower timeframes.
5. Rank surviving counts using logarithmic price proportion, duration, Fibonacci relationships, alternation, and parent-child consistency.
6. Use MACD, price-normalized EWO, comparable normalized volume, channels, and volatility only after the structural count exists. RSI remains disabled in the legacy default profile and is optional soft evidence under the adaptive `elliott_full` profile.
7. Keep causal pivot confirmation separate from degree selection; a confirmed pivot does not prove its degree label.
8. Store one canonical hierarchy and preserve all price-valid alternates in files that cannot overwrite it.
9. Do not change a user-selected price-valid canonical count unless it is invalidated or a newly requested blind recount documents materially stronger evidence.

This workflow applies to stocks, commodities, crypto, indices, and other markets. Confidence in the swing structure must be reported separately from confidence in the exact degree names.

## Wave-Boundary Indicator Audit

Before using RSI, volume, MACD, or EWO to confirm any Wave 3/Wave 5 relationship:

1. Lock the candidate wave pivots from price first and use one unchanged timeframe and data session.
2. Compare both the indicator value at each orthodox wave endpoint and the maximum/minimum value reached inside each complete wave segment. Never compare an internal peak in one wave with only the endpoint of another.
3. If Wave 5 RSI exceeds Wave 3 RSI, record `No RSI Divergence`. Do not claim exhaustion from RSI and do not move a pivot merely to manufacture divergence.
4. A higher Wave 5 RSI is not a classical invalidation. It triggers a recount of the internal subdivisions and explicit tests for an extended fifth, nested 1-2 structure, or incorrect degree assignment.
5. RSI, volume, MACD, and EWO rank structurally valid counts; price rules and valid internal subdivisions decide whether a count survives.
6. Every reported indicator comparison must include the timeframe, exact pivot dates/prices, and the measurement type used (`endpoint` or `within-wave extreme`).

This audit was reinforced by the NFLX daily recount beginning at the 14 June 2022 low of `$16.43`. In that local structure, `$48.50` is treated as a candidate Minor Wave 3 pivot rather than the end of Intermediate Wave (1); the first defensible completed Intermediate Wave (1) candidate extends to `$63.90` on 8 April 2024, subject to lower-timeframe subdivision verification.

## Nested 1-2 Extension Rule

Before accepting an unusually large or momentum-strong Wave 5, the AI must test a nested `1-2, 1-2, 1-2` interpretation:

1. Label from the largest pair inward; do not start from the vertical move.
2. Prove each Wave 1 is motive and each Wave 2 is corrective.
3. Require every bullish Wave 2 to remain above its corresponding Wave 1 origin.
4. Keep each smaller pair inside its parent Wave 3 boundaries.
5. Check for expanding normalized EWO/MACD, normalized volume, breadth where available, and channel acceleration as the third-of-a-third activates.
6. Require the final vertical advance to subdivide into five before calling its lower-degree Wave 3 complete.
7. After completion, expect Wave 4/5 pairs to unwind from the smallest degree outward.
8. Reject nesting when the proposed pairs are overlapping three-wave corrections better classified as W-X-Y.
9. Compare nesting explicitly against the extended-fifth count and preserve both if future structure is still needed to decide.

This rule was added after the MU recount showed that `$311.44 -> $1,254.81` was better treated as a nested third-wave impulse than as an oversized Minor Wave 5.

## Added From Latest Video

The AI must now apply these extra reading rules before accepting a count:

- Treat Elliott Wave degree as the chart dictionary.
- Identify same-degree labels first.
- Identify the largest visible degree before reading smaller labels.
- Lower timeframes are only zoomed-in detail of the higher timeframe; they must tie back to the parent anchors.
- Do not force a 1-2-3-4-5 impulse when price action is overlapping, equal-legged, or internally corrective.
- A trend can continue through repeated three-wave corrective structures, especially as WXY / double-three sequences.
- W/X and A/B are main connectors; once accepted, the next expected leg is Y or C, commonly toward an equal-leg area.
- Elliott Wave gives the probable path/map only. A trade still needs separate execution trigger, invalidation, and risk sizing.

## Strict Rule Change

Do not rely on the old simplified Elliott Wave video logic.
Do not accept a wave just because it "looks right."

A wave count must be treated as accepted only when:

- Mandatory Elliott price rules are satisfied.
- Internal subwaves match an allowed structure for that wave position.
- Lower-timeframe waves stay inside their parent wave boundaries.

Fibonacci, duration, alternation, channeling, wave personality, MACD/EWO, and volume rank the surviving counts and must be reported when they disagree. They are guidelines or supporting evidence, not independent invalidation rules.

If the internal structure cannot be proven, mark the wave as `Unproven Internal Structure` instead of forcing a count.

## Volume Verification

Each stored wave row can now receive `volume_confirmation_status` and `volume_notes`.
The verifier compares Wave 3 with Waves 1 and 5, checks volume contraction in Waves 2 and 4, and checks price/volume divergence between Waves 3 and 5.

Low Wave 3 volume is a warning, never a classical invalidation. The classical rule is that Wave 3 cannot be the shortest actionary wave by price movement and must travel beyond Wave 1.
Volume divergence raises reversal risk but cannot establish reversal timing without completed structure, price confirmation, and an invalidation level. At Primary degree and above, a fifth wave may naturally record greater volume than Wave 3 because long-term participation grows.

## Classical Book Integration

The Frost/Prechter reference now governs the rule hierarchy:

1. Degree is relative, not assigned from fixed price or time durations. Duration tables are heuristics only.
2. An impulse is `5-3-5-3-5`; Wave 2 cannot pass the origin of Wave 1; Wave 3 must pass Wave 1 and cannot be shortest; Wave 4 cannot overlap Wave 1 in a cash-market impulse.
3. Diagonals are motive exceptions. Ending diagonals occur mainly in Wave 5 or C and subdivide `3-3-3-3-3`; leading diagonals occur in Wave 1 or A and subdivide `5-3-5-3-5` under this reference. Wave 1/4 overlap is expected.
4. A correction is never complete as a five. Zigzags are `5-3-5`; flats are `3-3-5`; triangles are `3-3-3-3-3`; combinations are `W-X-Y` or `W-X-Y-X-Z`.
5. A five against the larger trend is only the first part of a correction, normally A of a zigzag or C in progress, not the whole correction.
6. Alternation, Fibonacci, equality, prior-fourth-wave support, channeling, volume, and personality are guidelines, not rules.
7. Use orthodox pattern endpoints for Fibonacci and duration measurement when an internal price extreme differs from the actual pattern termination.
8. Maintain preferred and alternate counts because multiple rule-valid interpretations commonly coexist in real time.

## Advanced Analytical Layers

The master database now includes four executable layers:

- EWO calculation from median price using `SMA(5) - SMA(35)`, plus completed-wave EWO and cumulative-volume aggregation.
- Five-wave impulse verification using Wave 3 strength, correction volume/EWO reset, and Wave 5 dual divergence.
- Early three-subwave Leg 1 classification as probable Wave A or Wave W using EWO depth, Weis effort/result, and volatility-band piercing.
- Correction forecasting for Zigzag, Flat, W-X-Y, and W-X-Y-X-Z structures.

Implementation: `scripts/elliott_analysis_layers.py`

Run the database migration and verification with the bundled Python runtime:

```powershell
& 'C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts\elliott_analysis_layers.py --database AI_BRAIN_MASTER_RULES.json --report advanced_analysis_report.json
```

EWO values must only be populated from raw, date-indexed OHLCV candles. Existing aggregate-only records remain `Insufficient Data` for impulse verification until their raw candle history is passed through `calculate_ewo()` and `aggregate_wave_metrics()`.

## Adaptive Pre-Freeze Recount

The opt-in adaptive recount supplements, but does not replace, the legacy
analysis path:

1. Select evidence resolution dynamically from immutable provider capability
   metadata. Timeframe granularity is not Elliott degree.
2. Ask for another native timeframe only through a typed structural question,
   with explicit request, bar, graph, pivot, node, depth, and terminal-degree
   budgets. The 100-140 bar view is a sampling target, never a wave rule.
3. Build Primary and Alternative from the same fresh price evidence in separate
   blind calls. Neither may see the other or any prior symbol-specific count,
   case, forecast, outcome, report, or lesson.
4. Reject a renamed duplicate Alternative by semantic graph comparison.
5. Validate immutable, completed, cutoff-safe native OHLCV before creating a
   pivot catalog. Missing coverage yields `not_covered`; it is not evidence
   against the count.
6. Reuse the lower-timeframe candidate generator and deterministic subdivision
   verifier one degree at a time. Only the verifier may assign `verified`,
   `unproven`, `not_covered`, or `inconsistent`.
7. Attach RSI, volume, EWO, MACD, Fibonacci, channel, duration, scale, and
   volatility evidence only after price boundaries exist. Under
   `elliott_full`, RSI is enabled through the canonical Wilder pipeline;
   low-level legacy defaults remain unchanged.
8. Keep provider, feed, exchange, session, timezone, adjustment, price basis,
   timeframe, and cutoff comparability explicit. Never silently mix feeds or
   treat derived candles as native.
9. Freeze the authoritative technical result only after the adaptive trace has
   explicit proof or bounded-stop states and the existing
   `resolve_degrees()` deterministic boundary succeeds.
10. Market Scenario and Phase 11 systems remain downstream and cannot rewrite
    the frozen technical result.

TradingView MCP is orchestrated through Codex only when its real tool can
return provenance-complete raw OHLCV. The currently audited
`mcp__codex_apps__tradingcursor_request_analysis` tool does not expose the
required candle/session/adjustment/pagination contract, so TradingView adaptive
ingestion must stop as unavailable rather than fabricate data or a client.

Implementation and operator workflow:
`docs/adaptive_elliott_recount_spec.md` and `$elliott-adaptive-recount`.

## Current DELL Revision

Daily and 4-hour subdivision validation on `2026-07-14` keeps DELL Intermediate Wave (3) as `active_candidate` but changes the lower-degree assignment. Minor Wave 1 is a probable completed impulse from `$64.84` to the strict adjusted-data extreme at `$166.83` (the submitted pivot was `$166.69`). Minor Wave 2 is a probable ABC zigzag ending at `$109.88`: B retraced about `50.04%` of A, and C reached approximately `0.614 x A`, close to the `0.618` projection.

Minor Wave 3 remains active. Minute Wave i is counted from `$109.88` to `$263.99`, Minute Wave ii is a probable regular Flat ending at `$227.27` on TradingView regular-session adjusted data, and Minute Wave iii is counted from `$227.27` to `$469.47`. The current correction is Minute Wave iv, not Minor Wave 4. Its observed A/B pivots are `$357.07` and `$460.50`, a roughly `92.02%` B retracement consistent with a Flat candidate. Minute Wave v, Minor Wave 4, and Minor Wave 5 remain ahead. A decline below `$263.99` would invalidate the standard impulse interpretation because Minute iv would overlap Minute i territory.
