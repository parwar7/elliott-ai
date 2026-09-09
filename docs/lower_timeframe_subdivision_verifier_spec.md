# Shadow-Only Lower-Timeframe Subdivision Verifier

## Status and Scope

The first single-graph deterministic verifier is implemented as a shadow-only
sidecar in `elliott_ai/lower_timeframe_subdivision_verifier.py`. It does not
authorize model calls, market-data retrieval, database writes, new forecasts,
or changes to existing degree resolutions. The later recursive
Monthly -> Weekly -> Daily -> native 4h bundle is implemented separately in
`elliott_ai/lower_timeframe_recursive_proof.py`; see
`docs/recursive_multi_timeframe_proof_bundle_spec.md` for its stricter
bottom-up policy.

The verifier answers one bounded question:

> Does the supplied immutable candidate parent/child graph have enough
> complete, compatible, lower-timeframe OHLCV evidence to support its declared
> internal family and pivots?

It does not create a new count, discover a count from scratch, choose between
primary and alternate counts, predict price, estimate probability, change a
target, or alter an existing resolution. It is a proof-or-non-proof sidecar,
not a new Elliott Wave model or automatic wave detector.

## Existing Architecture to Reuse

The implementation should reuse existing contracts and validation rules rather
than make a second technical-analysis system:

| Existing component | Planned use | Boundary |
|---|---|---|
| `forecast_records.DatasetCutoff` and `canonical_sha256` | Immutable source metadata, cutoff validation, canonical result hashing | No `ForecastRecord` is created. |
| `forecast_records.stored_record_content_hash` | Verify immutable source analysis-run and degree-resolution references | No source row is updated. |
| `technical_agent_orchestrator.TechnicalAgentRequest` and `CandidateWaveCount` patterns | Source/hash/cutoff discipline and frozen candidate handoff | The verifier is deterministic and is not a new model-facing `TechnicalAgentRole`. |
| `degrees.recompute_degree_hierarchy_verification` and `validate_degree_hierarchy_rules` | Existing parent-child, family, price-rule, and invalidation checks | The verifier adds evidence for a candidate; it does not rewrite the source response. |
| `correction_semantics` | Family parsing, `W-X-Y-X2-Z` handling, and motive/corrective family rules | Never infer a three- or five-wave family from the label alone. |
| `schema.DEGREE_TIMEFRAMES` and canonical anchor rules | Degree/timeframe compatibility and forward-compatible anchor normalization | Historical source records remain readable without rewriting them. |
| `market_data.load_candles`, metadata conventions, and segment coverage semantics | Native OHLCV reading and deterministic coverage facts | Do not use aggregated zoom bars as structural proof. |

`market_data.build_zoom_windows()` is useful for model refinement packets, but
it can rescale a long segment into an approximately 120-bar view. The verifier
must inspect only native, unaggregated rows from the immutable supplied files.
It may reuse the coverage concepts, but not an aggregated view as proof.

## Isolation and Pipeline Position

The verifier is an offline sidecar after a candidate graph has been frozen.
It is not called from `ElliottAgent.analyze()`, `ElliottAgent.resolve_degrees()`,
the normal CLI, Telegram, TradingView, the report command, a scheduler, or any
forecast workflow.

The intended manual sequence is:

1. Load one immutable analysis run and one immutable candidate graph from a
   stored degree resolution or already-frozen Phase 11D1 candidate.
2. Validate the exact decision-time cutoff, source hashes, and supplied OHLCV
   file hashes before reading any candles.
3. Build native lower-timeframe windows only from candles at or before that
   cutoff.
4. Run deterministic subdivision checks and return an immutable result.
5. Optionally present the result through a status-safe report adapter.

No verifier output is promoted into a degree resolution, `ForecastRecord`,
outcome evaluation, review, lesson, Phase 5 analogue ranking, or live analysis
pipeline. A later separately approved integration phase would be required for
any of those actions.

## Immutable Input Contracts

All contracts below are proposed as frozen, versioned, canonical-JSON records.
Their SHA-256 hash covers every field except the record's own `content_hash`.
They reject non-finite numbers, unknown enum values, dangling IDs, timestamps
after the cutoff, and source-hash mismatches.

### `LowerTimeframeVerifierRequest`

Required fields:

| Field | Meaning |
|---|---|
| `request_id`, `schema_version`, `policy_version`, `calculation_version`, `content_hash` | Immutable identity and reproducibility. |
| `shadow_mode` | Must be `true`; invocation must independently require `shadow_mode=true` too. |
| `source_analysis_run_id`, `source_analysis_run_hash` | Exact stored decision-time analysis source. |
| `source_degree_resolution_id`, `source_degree_resolution_hash` | Exact immutable resolution or frozen candidate source. |
| `analysis_cutoff_utc` | The exclusive upper bound for all usable evidence. |
| `candidate_graph` | One or more separately identified candidate hypotheses; never a mutable source-response reference. |
| `dataset_windows` | Native OHLCV window references with `DatasetCutoff` provenance and file hashes. |
| `verifier_policy` | Explicit recursion, timeframe, terminal-leaf, anchor, and resource limits. |
| `created_at_utc` | UTC creation time; it is not evidence availability. |

The request may reference a stored degree resolution or a frozen Phase 11D1
`CandidateWaveCount`. When the source is a model-generated resolution, its
labels and prose remain untrusted candidate assertions. Only typed anchors,
native candles, and deterministic checks can produce a verifier status.

### `CandidateWaveSnapshot` and `CandidateHypothesis`

A candidate snapshot is a normalized copy of a supplied wave node, not a
replacement for it. It must contain:

- stable candidate and source-wave IDs;
- parent ID and explicit child IDs;
- sequence position, degree, timeframe, direction, declared family, and
  completion state;
- typed start and end anchors;
- typed invalidation condition or an explicit legacy-unqualified state;
- source node hash and source provenance; and
- an optional untrusted source annotation kept outside validation fields.

`CandidateHypothesis` keeps the main candidate and every alternate separate.
It has an independent result. One invalid alternate must not erase a viable
main candidate, and one verified candidate must not silently delete an
alternate that remains structurally valid but unproven.

### `PivotAnchor`

Future candidate inputs should use one typed canonical anchor:

- `timestamp_utc`;
- finite numeric `price`;
- `price_field`: `high`, `low`, `open`, `close`, or `unknown`;
- source timeframe and dataset hash; and
- original source representation for audit only.

Historical date-only anchors remain accepted through a read-only adapter. A
date-only endpoint includes the entire endpoint trading day, matching the
existing refinement behavior. A historical anchor with no known price field
may be checked against that day's high-low range, but cannot be promoted to a
fully proved pivot merely because a matching price occurred during the day.
Absent a deterministic price-field/tick rule, that check is `unproven`, not
`inconsistent`.

### `NativeWindowReference`

Every supplied window must carry:

- its `DatasetCutoff` metadata and dataset/file hash;
- timeframe, provider, feed identity, symbol, exchange, regular-session rule,
  timezone, adjustment, and price-basis identity;
- first and last native candle timestamps, bar count, and completed-candle
  declaration;
- requested parent interval and inclusive-endpoint behavior; and
- a native-row manifest hash.

The verifier never fetches missing data. A row from another provider, session,
adjustment policy, or price basis is not silently mixed into a proof.

### `VerifierPolicy`

The policy is versioned and explicit. Its required controls are:

- a strictly descending timeframe ladder;
- mandatory source metadata compatibility rules;
- maximum recursion depth, maximum candidate nodes, and maximum native bars;
- minimum native-bar requirements for an attempted child proof;
- a terminal-leaf policy;
- anchor precision and price-field matching policy;
- allowed motive and corrective family rules; and
- whether a requested target degree requires another lower-timeframe proof.

The initial conservative profile should require an explicit `max_recursion_depth`
and reject values above a small fixed maximum. A proposed default is depth 3,
with a hard node cap of 125. Reaching a resource or depth bound returns
`unproven` with a deterministic reason code; it never turns a leaf into
`verified` by exhaustion.

## Deterministic Output Contracts

### `SubdivisionVerificationStatus`

Every candidate and every parent/child result has exactly one final structured
status:

| Status | Meaning |
|---|---|
| `verified` | All required native-data, pivot, family, ordering, price-rule, invalidation, timeframe, and recursively required child checks passed. |
| `unproven` | No hard contradiction was found, but a mandatory proof is absent, ambiguous, or stopped at an explicit terminal/resource boundary. |
| `not_covered` | No complete compatible lower-timeframe window is available for a required proof step. This is not a structural failure. |
| `inconsistent` | The supplied candidate contradicts a hard price, family, pivot-chain, invalidation, source-identity, cutoff, or timeframe rule. It rejects only that candidate hypothesis unless every valid alternative is also inconsistent. |

There is no fifth success-like state such as `likely`, `probable`, or
`model_verified`. Supporting check records use explicit boolean
`passed`, `required`, and `applicable` fields plus controlled reason codes;
they do not introduce a competing result status vocabulary.

### `SubdivisionCheck`

Each check has a stable check ID, scope, required flag, applicability flag,
boolean or unavailable result, controlled reason code, source evidence IDs,
window hashes, and comparison values. Checks cover:

- input and hash integrity;
- decision-time cutoff and completed-candle eligibility;
- full native-window coverage;
- feed/session/adjustment/price-basis consistency;
- typed pivot existence and source-bar compatibility;
- parent-child boundary equality and child-chain continuity;
- child order and expected position sets;
- motive/corrective family compatibility;
- hard impulse/diagonal and correction rules;
- typed invalidation condition; and
- recursive child verification requirements.

`SubdivisionVerifierResult` contains the source references, request hash,
per-hypothesis results, coverage records, checks, controlled reason codes,
required-next-evidence records, warnings, and a canonical content hash. It
contains no model narrative, confidence percentage, probability, target, or
trade recommendation.

## Verification Stages

### Stage 0: Shadow and Immutability Gate

The request and method invocation both require `shadow_mode=true`. Verify the
source analysis-run and resolution hashes with `stored_record_content_hash`,
verify every supplied file/window hash, and require all data cutoffs to be at
or before `analysis_cutoff_utc`. Reject any post-cutoff candle before forming
a window.

No provider is constructed. A test provider that raises on use should be used
in tests to prove this property.

### Stage 1: Candidate-Graph Normalization

Copy only the selected candidate hypothesis into typed immutable snapshots.
Validate unique IDs, parent/child references, positions, numeric anchors,
typed invalidations, permitted degrees, and declared timeframe. Convert
historical anchor strings through the existing backward-compatible parser, but
retain their legacy qualification state. Do not edit the analysis or degree
resolution record.

If a model node has an unsupported `Unassigned` control interval, it remains
outside the candidate graph. Origin and gap controls belong in existing
`hierarchy_metadata`, not normal wave validation.

### Stage 2: Native Data and Coverage Gate

For each parent interval, construct a window only from native candles from an
eligible supplied dataset. Full coverage requires all of the following:

1. the window begins on or before the parent start boundary and ends on or
   after the inclusive endpoint boundary;
2. all candles are marked completed and are at or before the decision cutoff;
3. trading-session gaps are explainable by the supplied exchange/session
   calendar or source metadata;
4. no duplicate or out-of-order timestamps are present; and
5. symbol, provider, feed, regular session, adjustment, and price basis match
   the candidate and parent proof chain.

The next verification timeframe must be strictly lower resolution than its
parent. The proposed default ladder is monthly, weekly, daily, 4h, 1h, and
15m. The verifier prefers the immediate eligible lower timeframe. It may not
skip an incompatible dataset and then describe the skipped proof as complete.
If a lower timeframe is absent or only partial, record `not_covered` for that
required step with the actual first/last timestamps and never call it missing
when coverage is full.

### Stage 3: Pivot and Boundary Proof

The verifier checks every supplied candidate anchor against the selected native
window using the declared price field and precision policy. It proves the
parent start equals the first child start, every child end equals the next
child start, and the last child end equals the parent end. A numerical mismatch
outside the declared source precision is `inconsistent`; a missing field,
unknown pivot basis, or no deterministically comparable row is `unproven`.

This stage validates supplied pivots. It does not scan prices and invent a
replacement pivot sequence.

### Stage 4: Family, Order, Price, and Invalidation Proof

Reuse `structure_family`, `MOTIVE_FAMILIES`, `CORRECTIVE_FAMILIES`,
`map_sequence_children`, `combination_variant`, and
`validate_degree_hierarchy_rules` for the established rules:

| Parent family | Required child positions | Required child families |
|---|---|---|
| Impulse or diagonal | `1-2-3-4-5` | 1, 3, 5 motive; 2, 4 corrective |
| Zigzag | `A-B-C` | A and C motive or allowed diagonal; B corrective |
| Flat | `A-B-C` | A and B corrective; C motive or allowed diagonal |
| Triangle | `A-B-C-D-E` | Every leg corrective |
| Double three | `W-X-Y` | Every component a valid corrective family; X is a corrective connector |
| Triple three | `W-X-Y-X2-Z` | Every component a valid corrective family; X and X2 remain distinct corrective connectors |

For motive candidates, verify connected pivots, Wave 2 origin protection,
Wave 3 extension, Wave 3-not-shortest, and the impulse overlap rule. A claimed
diagonal may use only approved diagonal exceptions. Overlap alone is never
proof of a diagonal. Until a full deterministic diagonal rule pack is approved,
a diagonal whose additional geometry cannot be checked remains `unproven`.

Typed invalidation conditions are evaluated on their declared basis. Existing
free-text invalidations are preserved but are not enough for a `verified`
result. They create an `unproven` condition rather than being reverse-parsed
into a new rule.

RSI, volume, EWO, MACD, Fibonacci, duration, channels, and model prose can be
attached as soft supporting, contradictory, unavailable, or incomparable
evidence. They never alter a hard structural check and cannot independently
produce `verified` or `inconsistent`. Incompatible intraday volume remains
unavailable or incomparable.

### Stage 5: Bounded Recursive Verification

The verifier walks only explicit child IDs. It carries an ancestry set to
reject cycles and decrements a fixed recursion budget at every descent. It
never creates descendants which were not supplied by the candidate graph.

A branch is a terminal leaf only when one of these explicit criteria applies:

- the policy's requested terminal degree has been reached and all proof
  obligations at that degree have passed;
- its allowed next lower timeframe is not included in the policy;
- no full compatible lower-timeframe window exists;
- the required native-bar minimum is not met; or
- the maximum depth/node/bar budget is reached.

The first condition can support `verified` only if no further child proof was
required by the policy. The other four stop conditions never create a verified
leaf: they return `not_covered` when data is unavailable or `unproven` when a
proof budget/definition is incomplete. This avoids infinite recursion and
prevents a resolution limit from being misrepresented as proof.

### Stage 6: Result Aggregation and Required Next Evidence

Results aggregate per hypothesis, not across main and alternate counts. A
hard contradiction gives that hypothesis `inconsistent`. A fully satisfied
recursive graph is `verified`. Required absent lower data is `not_covered`.
Any remaining viable but not fully proved graph is `unproven`.

Each non-verified result must include only controlled, structured next-evidence
requirements such as `provide_full_15m_window`, `provide_price_field_for_pivot`,
`provide_typed_invalidation`, or `supply_child_family_for_M33`. It must not
state a future market conclusion.

## Safe Presentation Rule

Structured verification status is the only source allowed to use the word
`verified` as a factual report assertion.

The current degree report renders `node.structure`, confirmations, concerns,
and alternate descriptions as raw text. Since that text may originate in a
model response, a future rendering adapter must not print a sentence such as
"verified impulse" for a node whose structured status is `candidate`,
`unproven`, or `not_covered`.

The required rendering behavior is:

| Structured state | Permitted standard-report wording |
|---|---|
| `verified` | "Deterministically verified" plus the family and check references. |
| `unproven` | "Candidate; deterministic subdivision remains unproven" plus reason codes. |
| `not_covered` | "Not covered at the required lower timeframe" plus exact coverage facts. |
| `inconsistent` | "Candidate is inconsistent with the stated deterministic rule" plus code. |

For any non-verified node, raw model structure/confirmation prose is omitted
from the ordinary report rather than rewritten as fact. It may remain in the
immutable source JSON and an explicitly labelled raw-audit view, but cannot be
used in the normal human-facing conclusion. Existing historical records remain
unchanged.

## Persistence and Migration Decision

No persistence is required for the first shadow-only implementation.

- The verifier returns an immutable, hashable in-memory result.
- It does not call `KnowledgeStore.create_*`, `save_degree_resolution`, or any
  Phase 11 ledger method.
- It does not use or modify `forecast_agent_orchestrations`, which remains
  exclusively owned by Phase 11D1 technical-agent orchestration.
- It does not create a `ForecastRecord`, observation set, outcome evaluation,
  review, lesson, checkpoint, or report artifact by default.
- It does not change `analysis_runs`, `degree_resolutions`, historical
  resolutions, or the active pipeline.

**Migration decision: no migration, zero new tables, zero altered tables.**

If durable verifier history later becomes useful, it needs a separately
approved additive ledger design. That later work must not reuse the D1
orchestration table and must preserve the same append-only, restricted-FK,
canonical-hash, and linear-supersession rules. It is out of scope here.

## Proposed Future File Surface

The first implementation is now available as a pure shadow-only, in-memory
module at `elliott_ai/lower_timeframe_subdivision_verifier.py`. It adds no
provider, database, migration, CLI, active-pipeline, forecast, or resolution
integration. The following is the implemented file surface for this approved
phase:

| File | Planned change | Not part of this design phase |
|---|---|---|
| `elliott_ai/lower_timeframe_subdivision_verifier.py` | New pure contracts, adapters, native-window checks, recursive verifier, and canonical result hashing | Yes |
| `elliott_ai/reporting.py` | Add a status-safe rendering adapter and stop raw model prose from asserting verification | Yes |
| `scripts/test_lower_timeframe_subdivision_verifier.py` | New focused deterministic verifier tests | Yes |
| `scripts/test_reporting_verification_language.py` | New report-language regression tests | Yes |
| `scripts/test_degree_verification_guardrail.py` | Extend existing guardrail fixtures for handoff compatibility | Yes |
| `elliott_ai/market_data.py` | No initial change; reuse readers and coverage concepts without using aggregated zoom views | No |
| `elliott_ai/degrees.py` | No initial change; reuse existing family and hard-rule validators | No |
| `elliott_ai/technical_agent_orchestrator.py` | No initial change; reuse frozen-contract patterns only | No |
| `elliott_ai/agent.py`, `elliott_ai/cli.py`, `elliott_ai/phase11_cli.py` | No change; no active-pipeline or command integration | No |
| `elliott_ai/knowledge.py` and SQLite schema | No change; no persistence/migration | No |

## Required Test Plan for a Future Implementation

Focused tests must cover at least:

1. Both explicit shadow-mode gates and a provider that fails if constructed or
   called.
2. Immutable request/result canonicalization, hash stability, and tamper
   rejection.
3. Source analysis-run/resolution hash mismatch, future candle, incomplete
   candle, and cutoff violation rejection.
4. Full, partial-before-start, partial-after-end, missing, incompatible-feed,
   incompatible-session, and incompatible-adjustment windows.
5. Date-only inclusive endpoint slicing, including the exact run-21-shaped I3
   situation: daily, 4h, and 1h full coverage; 15m partial coverage.
6. Native-row proof only; an aggregated/refined zoom view cannot satisfy a
   pivot or family check.
7. Valid and invalid impulse, diagonal handling, zigzag, flat, triangle,
   `W-X-Y`, and `W-X-Y-X2-Z` graphs, including distinct X and X2 IDs.
8. Numeric pivot mismatch, child order mismatch, parent boundary mismatch,
   discontinuous pivot chains, wrong degree/timeframe, and typed invalidation
   failures.
9. A label or raw model phrase saying "verified" cannot override failed or
   incomplete deterministic checks.
10. Bounded recursion, cycle detection, node/budget limits, terminal leaves,
    and no automatic descendants.
11. Separation of main and alternate results: one inconsistent count does not
    discard a separate viable unproven count.
12. RSI, volume, EWO, MACD, and Fibonacci evidence remain soft and cannot
    hard-invalidate or hard-verify a candidate.
13. Report rendering never calls a candidate/unproven/not-covered node
    "verified", even if its stored model prose contains that word.
14. No analysis run, resolution, forecast, database row, checkpoint, or
    existing Phase 11 orchestration changes after a shadow invocation.
15. Full existing Phase 11, degree-validation, and repository regression
    suites, plus SQLite integrity and foreign-key checks to prove the
    no-migration boundary remains intact.

## Open Design Decisions and Safe Defaults

The existing repository does not yet define all proof details below. The
verifier must fail closed rather than invent them:

| Open issue | Safe initial behavior |
|---|---|
| Historical anchors often lack `high`/`low`/`close` basis and tick precision | Accept as a candidate assertion; do not call the pivot verified. |
| Existing free-text invalidations are not typed conditions | Preserve text for audit; leave verification unproven. |
| A complete deterministic diagonal geometry rule pack is not yet approved | Do not prove a diagonal merely from allowed overlap. |
| The degree/timeframe matrix does not define every recursion depth target | Require an explicit policy terminal degree and maximum depth for each request. |
| Provider/exchange session-calendar details may be incomplete in older files | Treat coverage as not covered or data as inconsistent; never infer regular-session completion. |
| Candidate graph lacks explicitly supplied lower children | Return unproven; do not create a detector or invented subwaves. |

These controls are deliberate. They preserve valid alternatives, avoid
hindsight, and make the phrase "verified" mean a reproducible structural fact
rather than a model assertion.
