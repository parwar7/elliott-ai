---
name: elliott-adaptive-recount
description: Run or modify the repository's staged adaptive blind Elliott recount, including typed timeframe planning, Codex-controlled native OHLCV ingestion, independent Primary and Alternative generation, recursive subdivision proof, status-safe reporting, and explicit final technical freeze. Do not use for ordinary chart discussion that does not operate the adaptive workflow.
---

# Elliott Adaptive Recount

Follow `docs/adaptive_elliott_recount_spec.md` and use
`python -m elliott_ai adaptive-recount --help`. Preserve the existing
price-first Elliott rules and the authoritative `resolve_degrees()` boundary.

## Workflow

1. Run `adaptive-recount mcp-status` and inspect provider capabilities. Never
   infer an interval, feed, adjustment, session, completion flag, or row limit.
2. Run `adaptive-recount plan` with an exact UTC cutoff and a content-hashed
   capability set. Inspect the emitted `DataRequestManifest`.
3. For Codex-controlled ingestion, call only the actual provider tool named by
   the manifest. Write its unmodified completed native candles as a hashed
   `NativeOHLCVBundle`, then run `ingest`. If the tool cannot return raw OHLCV
   plus required metadata, stop as unavailable; analysis prose is not candles.
4. Generate Primary and Alternative roots only after deterministic data
   preflight and explicit model-call authorization. Keep each call blind to the
   other and to prior symbol counts, accepted cases, forecasts, outcomes, and
   lessons.
5. For every important parent, run `plan-proof`, acquire only the selected
   native timeframe, then run `prove`. Repeat one degree at a time within the
   plan's request, graph, pivot, bar, node, and recursion budgets.
6. Let only the deterministic verifier assign `verified`, `unproven`,
   `not_covered`, or `inconsistent`. Missing coverage is not evidence against a
   count. Preserve every structurally valid unresolved alternative.
7. Run `ready` and `report`. Only after all branches have an explicit proof or
   bounded stop may `finalize` call the normal analysis and
   `resolve_degrees()` persistence path. Require explicit authorization for
   both model calls and `--commit`.

## Guardrails

- Indicators are soft post-boundary evidence. Compare RSI, volume, EWO, and
  MACD only on compatible feed, timeframe, session, adjustment, and cutoff.
- Never fabricate or scrape TradingView data, silently mix providers, use an
  incomplete candle, promote derived data as native, or adopt an aggregated
  bucket boundary as an orthodox pivot without native confirmation.
- Never hard-code RKLB pivots or counts, retrieve prior RKLB analysis in blind
  mode, mutate a frozen candidate, or allow model prose to override proof.
- Do not activate Phase 11, market scenarios, forecasting, Telegram, trading,
  or database migrations. Phase 11 remains downstream and human-gated.
- Run focused adaptive tests, the complete repository suite, compilation, and
  read-only SQLite integrity checks after code changes.
