# Session-Aligned Derived 4h Proof Evidence

## Status and Scope

This is a shadow-only, in-memory extension to the existing recursive
multi-timeframe proof bundle. It does not fetch market data, call a model,
write SQLite, create a checkpoint, modify historical resolutions, add CLI
commands, or participate in the active analysis pipeline.

`native_4h` and `derived_4h` are distinct evidence kinds:

- `native_4h` remains the existing provider-native proof path.
- `derived_4h` is a deterministic reconstruction from immutable,
  provider-native intraday source rows. It is never reported as native data.

The existing native verifier and its policy are unchanged. A caller must opt
into `SessionAlignedDerived4hProofVerifier` with an immutable overlay.

## Session-Aligned Meaning of 4h

For this policy, a 4h proof bar is a regular NASDAQ session-aligned bucket
with a maximum duration of four hours. It is not required to be exactly four
hours.

For a normal 09:30–16:00 America/New_York session, the valid buckets are:

1. 09:30–13:30, a four-hour bucket.
2. 13:30–16:00, a 2.5-hour terminal shortened bucket.

For an early close, the final bucket ending at the official close is valid
when it is shorter than four hours and complete. DST is handled entirely by
the caller-supplied UTC session schedule; the implementation does not infer
calendar dates or close times from gaps in source data.

Every reconstructed bar records its UTC start/end, duration, duration class,
terminal status, session ID, source candle IDs, source-row hashes and source
window rows hash. A terminal shortened bar is accepted only when it starts at
the preceding bucket boundary, ends exactly at the official session close,
contains every required underlying interval, and never crosses a session.

## Immutable Inputs

The reconstruction request contains:

- a completed, provider-native intraday `NativeOHLCVWindow`;
- one explicit `IntradaySourceInterval` for every source candle, including
  `bar_start` or `bar_end` timestamp semantics;
- a versioned immutable `NasdaqSessionSchedule`, including calendar source and
  calendar version;
- the versioned `SessionAligned4hAggregationPolicy`;
- the decision-time cutoff.

The first approved policy is `nasdaq-session-aligned-max-4h-v1` version
`1.0.0`. It permits 1m, 5m, 15m, 30m and 1h provider-native source rows,
regular NASDAQ sessions, America/New_York market metadata, and terminal
shortened buckets.

Each input and output has canonical JSON and a SHA-256 content hash. The
derived dataset preserves provider, feed identity, symbol, exchange, session,
timezone, adjustment policy, price basis and cutoff from the source dataset.

## Reconstruction Rules

1. Validate source rows, hashes, completed-candle state, source timeframe,
   cutoff, market-data identity, timestamp semantics and schedule metadata.
2. Reject a source interval that is unknown, duplicated, mismatched, overlaps
   another interval, extends past the cutoff, or crosses a scheduled session.
3. Tile each supplied session into policy buckets, without combining rows from
   separate sessions.
4. Require contiguous explicit source intervals to cover every bucket exactly.
   No interpolation, gap repair, resampling from daily data, or inferred rows
   is permitted.
5. Aggregate OHLCV only after the bucket is fully covered: first open, maximum
   high, minimum low, final close and summed volume.
6. Build the immutable derived window and its pivot catalog only after every
   source row has exactly one valid derived-bar lineage.

Missing source timestamps, rows or bucket intervals return `not_covered`.
Malformed, tampered, overlapping, cross-session, cutoff-violating or
market-data-incompatible input returns `inconsistent`.

## Proof Adapter

`SessionAlignedDerived4hProofVerifier` is an opt-in subclass sidecar. It uses
the existing recursive verifier for Elliott family checks, child order,
connected pivots, hard price rules, invalidations and bottom-up propagation.
It replaces only explicitly bound terminal 4h evidence or a Daily-to-4h child
graph with `derived_4h` evidence.

The overlay must be shadow-only, reference the exact immutable bundle, contain
at least one reachable-node binding, and use the same policy hash as every
reconstruction result. A terminal derived 4h node is valid only beneath a
Daily node with an explicit derived child-graph binding. That Daily binding in
turn must include every one of its direct terminal 4h children. A partial
derived branch is `inconsistent`; it may not fall back to native 4h rows. A
Daily child graph must also carry a
`Derived4hCandidateGraphBinding` with the exact graph ID, graph hash, policy
ID, policy version and policy hash. A graph generated under one aggregation
policy cannot be verified under another.

The adapter is the only new path that can emit the distinct reason code
`terminal_derived_4h_verified`. It does not change the meaning of native proof
status. A model label or prose statement cannot mark either kind as verified.

## Status Mapping and Reporting

The verifier returns only the existing structured proof statuses:

- `verified`: all required lower-timeframe evidence and hard rules pass.
- `unproven`: structure, descendants, recursion limit or proof scope is
  insufficient, but the supplied evidence is not malformed.
- `not_covered`: compatible complete source intervals are missing.
- `inconsistent`: data, provenance, schedule, policy, pivots, boundaries or
  hard structural rules conflict.

`derived_4h` evidence carries explicit provenance. When a report node includes
`recursive_multi_timeframe_proof_evidence_kind: "derived_4h"`, reporting adds
`derived_4h session-aligned aggregation`; it never calls that evidence native.
Structured proof status controls all verified/unproven wording, so raw model
prose cannot promote candidate evidence.

## Public APIs

- `elliott_ai.session_aligned_derived_4h.reconstruct_session_aligned_4h`
- `elliott_ai.session_aligned_derived_4h.build_derived_4h_pivot_catalog`
- `elliott_ai.session_aligned_derived_4h.build_derived_4h_coverage_attestation`
- `elliott_ai.session_aligned_derived_4h.bind_candidate_graph_to_derived_4h`
- `elliott_ai.session_aligned_derived_4h_proof.SessionAlignedDerived4hProofVerifier`

All APIs are pure in-memory operations. Callers retain responsibility for
acquiring trusted source rows and official session schedules outside this
extension.

## Deferred Work

This phase deliberately does not add provider adapters, an exchange-calendar
fetcher, historical data acquisition, persistence, CLI integration,
candidate-generation changes, active-pipeline integration, or a run against
historical GOOGL data.
