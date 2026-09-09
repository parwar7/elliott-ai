# Recursive Multi-Timeframe Proof Bundle

## Status

The first implementation is available as the shadow-only, in-memory module
`elliott_ai/lower_timeframe_recursive_proof.py`. It has no model call,
market-data request, SQLite operation, CLI command, checkpoint, forecast, or
active-pipeline integration.

Its sole question is narrow:

> Does one already-frozen candidate hierarchy have deterministic native-data
> proof through every required lower timeframe?

It does not generate a count, choose a primary count, alter an Elliott degree,
change a historical resolution, predict prices, or make a trade decision.

## Fixed Proof Ladder

The initial policy is fixed and versioned:

```text
Monthly parent
  -> Weekly required children
    -> Daily required children
      -> Native 4h required children
```

`proof_timeframe` is an evidence property, independent of Elliott degree. For
example, a Cycle wave can be examined with monthly proof while its required
children are weekly; a Primary wave can be examined with weekly proof. The
existing degree-to-timeframe matrix is not changed.

The verifier works strictly bottom-up:

```text
native 4h verified -> Daily verified -> Weekly verified -> Monthly verified
```

A monthly node can never become `verified` from monthly data alone. Each
required child must be explicit, have complete compatible native coverage, and
itself reach the required lower proof level.

## Immutable Inputs

Each `RecursiveProofBundle` represents exactly one hypothesis: `primary` or
`alternative`. The two bundles are immutable and are verified independently.
One result never deletes, promotes, or changes the other.

The bundle contains:

- frozen source analysis-run and degree-resolution IDs and hashes;
- one explicit root node;
- typed `ProofWaveSnapshot` objects with numeric pivots and typed
  invalidations;
- a distinct `proof_timeframe` for every node;
- native OHLCV windows and deterministic pivot catalogs;
- explicit `ProofCoverageAttestation` records for each window interval;
- candidate-only direct child graphs; and
- a canonical SHA-256 content hash for every contract.

Candidate graphs may originate from a model in a later caller, but they are
only candidate assertions. They have no verification-status field. A graph
which tries to set `verified_to_timeframe`, or whose child state is not
`candidate`, is rejected by the deterministic verifier.

## Status Semantics

Only `SubdivisionVerificationStatus` is used:

| Status | Meaning |
|---|---|
| `verified` | Completed node, native rows and coverage attestations pass, pivots and boundaries match, family and hard rules pass, and every required descendant is verified. |
| `unproven` | Candidate remains structurally viable but a required descendant, proof rung, or permitted resource budget is missing or incomplete. |
| `not_covered` | Complete compatible native historical data or a required coverage attestation is unavailable. |
| `inconsistent` | A hard source, pivot, boundary, family, price-rule, invalidation, cutoff, or candidate-only rule is violated. |

The following always remain `unproven`, never verified: missing descendants,
skipped Monthly/Weekly/Daily/4h proof rungs, node-limit exhaustion, active or
projected parents, unsupported diagonal geometry, and an unexpected descent
beyond the approved terminal 4h rung.

The following return `not_covered`: absent native window, absent coverage
attestation, incomplete coverage, incomplete bar interval, or incompatible
historical metadata where no compatible replacement is supplied.

## Deterministic Checks

At every rung, the verifier checks:

1. completed, cutoff-safe native rows and canonical hashes;
2. provider, feed, symbol, exchange, session, timezone, adjustment, and price
   basis compatibility across the proof chain;
3. an explicit coverage attestation that spans the parent interval;
4. typed pivot catalog lineage and source-row values;
5. exact timestamp/price boundary connection between parent and first/last
   child and between adjacent children;
6. family child-position and motive/corrective semantics;
7. motive direction, Wave 2 origin protection, Wave 3-not-shortest, and
   impulse overlap hard rules;
8. typed invalidation source and breach checks; and
9. all required child-node identities and their bottom-up results.

RSI, volume, EWO, MACD, Fibonacci, duration, channels, model prose, and
confidence are not inputs to verification status. They remain separate soft
evidence in the existing system.

## Resource and Safety Gates

- `shadow_mode=True` is required both in the immutable bundle and the method
  invocation.
- The node cap applies independently to each hypothesis bundle. Hitting it
  returns `unproven`; it cannot make a leaf or parent verified.
- The verifier walks supplied child IDs only. It never discovers, repairs, or
  invents a child graph.
- Cycles and dangling graph references are rejected or retained as unproven as
  appropriate; no automatic repair occurs.
- The initial terminal evidence timeframe is native 4h. One-hour data can be
  added only through a separately approved policy; it is not silently used.
- Primary and Alternative trees must be supplied separately. They do not share
  a result object or a mutable candidate graph.

## Safe Presentation

`elliott_ai/reporting.py` gives
`recursive_multi_timeframe_proof_status` precedence over the older
single-graph subdivision field. If the recursive result is `unproven`,
`not_covered`, or `inconsistent`, raw model text cannot be rendered as a
verified structure.

## Public API

```python
from elliott_ai.lower_timeframe_recursive_proof import (
    RecursiveMultiTimeframeProofVerifier,
    RecursiveProofBundle,
    RecursiveProofBundlePair,
    monthly_to_4h_recursive_proof_policy,
)

result = RecursiveMultiTimeframeProofVerifier().verify(
    frozen_primary_bundle,
    shadow_mode=True,
)

pair_result = RecursiveMultiTimeframeProofVerifier().verify_pair(
    frozen_primary_and_alternative_pair,
    shadow_mode=True,
)
```

These methods accept no provider, model, database, or network argument.

## Explicit Non-Goals

This phase does not add a live generator, model call, market-data fetch, CLI,
database table, persistence method, resolution write, forecast, report command,
Telegram action, TradingView action, or active pipeline invocation. It does not
run GOOGL or modify immutable resolution records.
