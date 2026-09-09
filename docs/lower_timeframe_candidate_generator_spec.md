# Shadow-Only Lower-Timeframe Candidate Generator

## Status and Purpose

The first implementation is now available as a shadow-only, in-memory module
at `elliott_ai/lower_timeframe_candidate_generator.py`. It keeps the boundaries
defined here: no bundled live provider, no live model call without explicit
caller authorization, no market-data request, database write, migration, CLI,
report, or active-pipeline integration. Tests use fake structured providers
only.

The Lower-Timeframe Candidate Generator is the companion to
docs/lower_timeframe_subdivision_verifier_spec.md. Its limited job is to
produce two independently frozen possible child graphs for one already-frozen
parent candidate. The Deterministic Subdivision Verifier remains the only
component permitted to decide whether a candidate is verified, unproven,
not_covered, or inconsistent.

The generator does not discover a new parent count, promote a child count,
select a winner, replace an existing degree resolution, make a forecast, or
make a trading decision. It provides structured candidate input for a later
verifier call only.

## Architecture Boundary

The intended shadow-only flow is:

1. A parent candidate is already frozen by a stored degree resolution or frozen
   Phase 11D1 candidate.
2. Deterministic preflight validates cutoff, source hashes, native OHLCV window
   hashes, target timeframe, session, feed, adjustment, and price basis.
3. A deterministic pivot catalog is built from the supplied native window.
4. The Primary Child Graph Generator and Alternative Child Graph Generator
   receive the same frozen packet while remaining blind to each other.
5. Each generator returns one typed candidate graph or structured generation
   failure. Neither can return a verification status.
6. Both outputs are schema-validated, frozen, canonically hashed, and checked
   for material duplication.
7. The Lower-Timeframe Subdivision Verifier receives each frozen graph
   separately and is the only stage allowed to assign a verification status.
8. A deeper request, if desired, uses an explicitly selected child candidate as
   a new immutable parent. It never mutates the graph that created it.

There is no final selection or automatic reconciliation stage. ElliottAgent,
stored degree resolutions, forecasts, outcomes, reviews, mistake memory, Phase
5 analogue retrieval, Telegram, TradingView, CLI, and reporting remain outside
this design.

## Existing Components to Reuse

| Existing component | Planned reuse | Explicit non-use |
|---|---|---|
| forecast_records.DatasetCutoff | Cutoff, source, feed, session, adjustment, price-basis, and completed-candle provenance | No ForecastRecord is created. |
| forecast_records.canonical_sha256 and stored_record_content_hash | Canonical hashes and immutable source-record identity | No persistence method is called. |
| technical_agent_orchestrator request/candidate patterns | Paired blind-agent ordering, strict typed output, source-reference discipline | Do not change the four Phase 11D1 roles or their table. |
| market_data.load_candles and native pivot-ladder conventions | Read supplied native OHLCV and make source-limited pivot candidates | Do not use aggregated build_zoom_windows bars as evidence. |
| schema.STANDARD_DEGREES and DEGREE_TIMEFRAMES | Require one-degree-lower child targeting and valid timeframes | Do not change the existing matrix. |
| correction_semantics | Family vocabulary and X versus X2 semantics | Do not infer a family solely from a wave letter. |
| Lower-Timeframe Subdivision Verifier | Receives frozen graphs and performs all structural proof | Generator cannot copy, override, or reinterpret verifier results. |

The existing TechnicalAgentRole enum and forecast_agent_orchestrations ledger
remain exclusively Phase 11D1 assets. The generator may follow their immutable
contract pattern but must not be inserted into that role enum or table without
separate approval.

## Non-Negotiable Boundaries

The generator must never:

- emit verified, unproven, not_covered, or inconsistent as a graph, node,
  evidence, or execution result;
- set a child node to confirmed, set a structural-verification field, or create
  a final-readiness assertion;
- create, update, supersede, or delete an analysis run, degree resolution,
  forecast, observation set, review, lesson, checkpoint, or database row;
- fetch data, use post-cutoff candles, use news/fundamentals, retrieve mistake
  memory, or inspect historical outcomes;
- see the opposing generator graph before its own output is frozen;
- repair a parent, invent a missing pivot, silently change a source price, or
  use model prose as a proof; or
- automatically descend into grandchildren, choose a child for descent, or
  pass a graph into the active pipeline.

Every generated child node is a candidate assertion until the separate
deterministic verifier assesses it.

## Proposed Immutable Contracts

All proposed contracts are frozen, versioned canonical JSON. Their
content_hash is SHA-256 over the complete canonical payload except the record's
own hash. They reject unknown fields, non-finite numbers, duplicate IDs, future
timestamps, unsupported target timeframes, and source-hash mismatch.

### LowerTimeframeCandidateGenerationRequest

Required fields:

| Field | Meaning |
|---|---|
| request_id, schema_version, policy_version, calculation_version, content_hash | Reproducible request identity. |
| shadow_mode | Must be true; future invocation independently requires true. |
| source_analysis_run_id, source_analysis_run_hash | Immutable decision-time analysis source. |
| source_degree_resolution_id, source_degree_resolution_hash | Immutable resolution or frozen D1 candidate source. |
| parent_candidate | One immutable ParentCandidateSnapshot. |
| analysis_cutoff_utc | Exact decision-time maximum; every usable completed candle must be at or before it. |
| target_child_degree, target_timeframe | Exact one-degree-lower objective. |
| native_ohlcv_scope | Cutoff-safe native window references, hashes, and metadata. |
| pivot_catalog | Deterministically constructed candidates from the same native data. |
| proof_scope | Terminal degree, target timeframe, and known coverage limits. |
| generation_policy | Explicit limits, blind-mode rules, permitted families, and policy ID. |
| created_at_utc | Creation time only; never evidence availability. |

The request contains no earlier child graph, unbounded report prose,
outcome/review/lesson record, fundamentals, news, market scenario, or
post-cutoff row. An adapter may build it from stored records only by copying the
exact parent, source hashes, native windows, and permitted decision-time data.

### ParentCandidateSnapshot

This is a typed immutable copy of one supplied parent candidate. It contains:

- source and candidate IDs plus source-node hash;
- degree, timeframe, declared family, direction, completion state, and sequence
  position;
- typed start/end anchors and a parent-level typed invalidation assertion;
- the immediately lower child degree expected for this request;
- parent cutoff, feed, session, adjustment, and price-basis identity; and
- optional raw-source annotation kept outside structural fields.

The parent must have a standard degree, known direction, finite numeric anchors,
and compatible timeframe. A historical legacy anchor may be adapted read-only,
but missing price-field or typed-invalidation details remain explicit
limitations, not proof.

### NativeOHLCVScope and PivotCatalog

NativeOHLCVScope identifies supplied native data without fetching or resampling.
It has one required target-timeframe window and optional supporting windows.
Every window contains:

- DatasetCutoff-compatible metadata;
- file/dataset and native-row-manifest hashes;
- timeframe, first/last timestamp, bar count, and completed-candle state;
- symbol, exchange, provider, feed, regular-session, timezone, adjustment, and
  price-basis metadata; and
- coverage role: required_generation, supporting_only, or policy_not_required.

The required window must fully cover the parent interval through the inclusive
endpoint boundary. Partial, cross-feed, cross-session, adjustment-incompatible,
or after-cutoff data cannot be silently used. It produces a generator failure,
not a verifier status.

The deterministic PivotCatalog is prepared before either generator receives the
packet. Each typed pivot has a stable ID, UTC timestamp, finite price, source
field (high, low, open, or close), source bar hash, pivot type, timeframe, and
source-window hash. A generator may select only catalog IDs, never raw
replacement timestamps or prices. Catalog membership is evidence lineage, not
Elliott verification.

### CandidateInvalidation and CandidateProofScope

Each child has a typed CandidateInvalidation with finite threshold, comparison
direction, evaluation basis, scope, and source pivot or price reference. The
generator proposes it only; it does not evaluate the condition.

Each graph carries CandidateProofScope:

| Field | Generator requirement |
|---|---|
| target_timeframe | Explicit native timeframe used for this one-degree-lower graph. |
| verified_to_timeframe | Always null in generator output. It is reserved for the verifier result. |
| terminal_degree | Deepest degree allowed by the request chain before a new explicit request. |
| coverage_limitations | Controlled unavailable, partial, supporting-only, or policy-not-required records. |

Only the deterministic verifier may replace verified_to_timeframe null with a
non-null proof boundary. The generator cannot describe that boundary as
achieved.

### GeneratedChildWave and CandidateChildGraph

A graph consists solely of one-degree-lower child candidates. It contains:

- graph ID, parent candidate ID, role (primary or alternative), and graph hash;
- target child degree and target timeframe;
- declared parent family and typed child-family declarations;
- ordered child IDs and permitted sequence positions;
- start/end pivot IDs and resolved typed anchors for every child;
- typed invalidation for every child;
- candidate_state fixed to candidate; and
- CandidateProofScope, source hashes, and controlled evidence IDs.

For a reconstruction of an already frozen parent, the graph's declared parent
family must exactly equal the frozen parent's declared family. Different
catalog pivots, segmentation, split/merged swings, and permitted child
subfamilies may be proposed within that contract. A different parent family is
a separate immutable parent hypothesis, not an alternative child anatomy; the
current recursive generator returns `parent_family_reclassification_required`
instead of silently relabelling the parent.

No graph or node contains degree_status confirmed, a verification status,
verification reason code, confidence score, price target, trade instruction, or
free text that asserts proof.

### CandidateGenerationPair and CandidateGenerationFailure

A successful envelope has exactly two independent slots: primary_graph and
alternative_graph. Graph hashes must differ. A normalized graph signature
checks that the alternative is materially different, not a renamed duplicate.

If one role is malformed, uses unknown pivots, has unsupported positions, lacks
typed invalidations, duplicates the other graph, or fails a source contract,
record a CandidateGenerationFailure for that role. Do not silently copy the
primary graph or invent a replacement.

Failure records use generator-only codes such as missing_full_target_window,
target_timeframe_not_lower, pivot_not_in_catalog, family_positions_invalid,
duplicate_graph_signature, recursion_budget_exhausted, or
malformed_structured_output. They must never use the verifier's four statuses.

## One-Degree-Lower Rule and Family Constraints

Each request moves from one parent degree to the immediately lower standard
Elliott degree. It may not skip a degree or produce grandchildren in the same
call. An Intermediate parent can generate a Minor graph; a later separate
request can use one frozen Minor candidate to generate a Minute graph.

The graph declares but does not prove its family:

| Declared family | Permitted positions |
|---|---|
| Impulse or diagonal candidate | 1-2-3-4-5 |
| Zigzag or flat candidate | A-B-C |
| Triangle candidate | A-B-C-D-E |
| Double-three candidate | W-X-Y |
| Triple-three candidate | W-X-Y-X2-Z |

The generator does not infer a three- or five-wave family from a letter. The
Subdivision Verifier later applies the family, pivot, invalidation, and hard
price-rule checks.

## Agent Ordering and Blindness

The future model-backed version is a bounded paired generator, not an
authoritative resolver:

1. Parent freeze: caller supplies one immutable parent and cutoff-safe OHLCV.
2. Deterministic preflight: validate hashes, cutoff, full required coverage,
   target timeframe, metadata, degree relation, and pivot catalog.
3. Primary Child Graph Generator: receives only the frozen packet and produces
   one primary graph using catalog pivot IDs.
4. Alternative Child Graph Generator: receives the same packet but never the
   primary output; produces one distinct graph or failure.
5. Freeze and schema gate: canonicalize, hash, validate graphs, and reject
   duplication.
6. Subdivision Verifier: independently assesses each frozen graph and is first
   and only status-bearing stage.
7. Explicit next descent: a human or separately approved coordinator names one
   frozen child as a new parent. No auto-repair or automatic graph chasing.

No mistake memory is visible to either generator. A later approved auditor could
receive cutoff-valid lessons only after both graphs are frozen and only as
non-binding audit context; it cannot edit, rank, or select a graph.

## First GOOGL Policy: Intermediate to Minor to Minute

Proposed policy ID: googl-lower-timeframe-candidate-policy-1.0.0.

This policy defines the allowed proof path only. It does not assert that a GOOGL
wave is complete or verified.

| Step | Parent degree | Generated child degree | Required generation timeframe | Lower-data role |
|---|---|---|---|---|
| 1 | Intermediate | Minor | Daily | Required native child-graph generation data. |
| 2 | Selected Minor | Minute | 4h | Required data in a separately requested descent. |
| Supporting | Minute or selected Minor context | None | 1h | Supporting pivot/evidence context only; not required. |
| Excluded for early-2025 structure | Any | None | 15m | policy_not_required; absence cannot block the Daily/4h path. |

For each output under this policy:

- terminal_degree is Minute;
- verified_to_timeframe is null;
- a Daily Intermediate-to-Minor graph records 4h as later permitted descent,
  not completed proof;
- a 4h Minor-to-Minute graph records 1h as supporting_only; and
- 15m appears in coverage_limitations as
  policy_not_required_for_early_2025, not as missing required proof.

Global maximum generation depth is two: Intermediate-to-Minor, then
Minor-to-Minute. Each request produces at most a primary and alternative graph.
The system must not automatically descend into every Minor child because that
creates a combinatorial tree and falsely implies all subdivisions were generated
or proved. A new descent names one frozen Minor candidate explicitly.

## Recursion, Resource Limits, and Fail-Closed Behavior

The generator never recurses inside one call. A separate coordinator may make a
bounded request chain only with:

- maximum depth, fixed at two for the first GOOGL policy;
- strictly decreasing degree and timeframe;
- an ancestry set rejecting parent/child cycles;
- at most two graph outputs per request;
- maximum native-bar and pivot-catalog size; and
- hard stop at terminal_degree.

Reaching a budget, terminal degree, missing target window, or incomplete parent
source cannot make the generator invent a graph or issue a verifier-like
status.

| Condition | Required generator behavior |
|---|---|
| Source record/hash or cutoff mismatch | source_integrity_failed. |
| After-cutoff/incomplete/wrong-session/wrong-feed/wrong-adjustment data | Reject required target window. |
| Target timeframe is not lower or policy-authorized | target_timeframe_not_lower. |
| Target window partly covers parent | missing_full_target_window, never not_covered. |
| No deterministic pivot catalog | pivot_catalog_unavailable. |
| Raw/invented pivot, unknown ID, or non-finite number | Reject that role output. |
| Model uses a verifier status word | Reject as schema-forbidden. |
| Primary and Alternative are materially identical | Preserve primary; record duplicate alternative failure. |
| Family/links/invalidation malformed | Reject graph before verifier handoff. |
| Deeper request lacks explicit frozen child selection | Reject; never auto-descend. |
| Provider failure or malformed structured response | Preserve failure trace; never fabricate a graph. |

RSI, volume, EWO, MACD, Fibonacci, duration, channels, and model explanation
text may be bounded supporting metadata only. They cannot create a pivot, change
a family, remove an alternative, prove a graph, or reject a graph under a hard
Elliott rule.

## Persistence and Migration Decision

The first implementation needs no durable storage.

- Results are immutable, canonically hashed return objects only.
- No database method is called and no SQLite table is created.
- forecast_agent_orchestrations is neither reused nor modified.
- No analysis run, degree resolution, forecast, observation set, outcome,
  review, lesson, checkpoint, or active report changes.

Migration decision: zero migrations and zero database changes.

If persistent candidate-generation traces later become useful, they require a
separate approved append-only ledger with restricted foreign keys, canonical
hashes, and linear supersession. That work is deferred.

## Exact File Surface for a Future Implementation

The only file changed in this design phase is this document. A later approved
implementation should have this limited surface:

| File | Future change |
|---|---|
| elliott_ai/lower_timeframe_candidate_generator.py | New immutable contracts, preflight, pivot-catalog adapter, paired generator interface, graph validation, and canonical hashing. |
| elliott_ai/lower_timeframe_subdivision_verifier.py | Accept generated graph/proof-scope contracts and become the sole writer of non-null verified_to_timeframe plus the four statuses. |
| scripts/test_lower_timeframe_candidate_generator.py | New focused candidate-generation tests. |
| scripts/test_lower_timeframe_subdivision_verifier.py | Producer-to-verifier handoff, proof-scope, and status-ownership tests. |
| docs/lower_timeframe_candidate_generator_spec.md | This design document. |
| elliott_ai/technical_agent_orchestrator.py | No initial modification; patterns only. |
| elliott_ai/agent.py, elliott_ai/cli.py, elliott_ai/phase11_cli.py | No modification; no pipeline or command integration. |
| elliott_ai/knowledge.py and SQLite schema | No modification; no persistence or migration. |
| elliott_ai/reporting.py | No generator-specific change; verifier design separately owns safe wording. |

## Required Future Tests

A future implementation must test:

1. Immutable request, graph, proof-scope, and result serialization/hash
   stability and tamper rejection.
2. Explicit shadow_mode at both request and invocation boundaries.
3. No provider construction during deterministic preflight and no model call
   after a preflight failure.
4. Source hash, cutoff, completed-candle, session, feed, adjustment, price
   basis, timestamp-order, and native-window validation.
5. One-degree-lower and policy-authorized target timeframe only; no skipping.
6. Native-only pivot catalogs, typed-pivot lineage, and rejection of invented
   raw anchors.
7. Primary/Alternative blindness, distinct hashes, duplicate detection, and no
   automatic fallback copy.
8. Candidate-only nodes with no confirmation/verification fields.
9. Required family, child order, pivots, invalidations, terminal_degree,
   coverage_limitations, and verified_to_timeframe null.
10. Only a fake deterministic verifier can populate non-null
    verified_to_timeframe or issue the four verifier statuses.
11. First GOOGL policy behavior: Intermediate-to-Minor Daily; separately
    selected Minor-to-Minute 4h; optional 1h; non-required 15m.
12. Recursion/depth/node/pivot limits, cycle rejection, explicit descent, and
    terminal-degree stop.
13. Partial target windows produce generator failures rather than verifier
    statuses.
14. No database row, source record, forecast, checkpoint, active call site, or
    Phase 11 orchestration row changes after execution.
15. Existing verifier, degree guardrail, Phase 11, and full repository suites
    remain compatible; SQLite integrity and foreign-key checks remain clean.

## Open Decisions and Safe Defaults

| Open point | Safe decision |
|---|---|
| Model-backed pair versus later deterministic enumerator | Keep contracts provider-neutral. Any model use stays shadow-only and needs explicit approval. |
| Legacy tick tolerance and pivot price field | Use only catalog pivots with typed source fields; expose limitation rather than guess. |
| Whether every Minor gets a Minute request | Require explicit human or separately approved coordinator selection. |
| Persistent shadow trace | Do not persist in first phase; require a new approved ledger design. |
| 1h requirement for another period or symbol | Make it policy-versioned. First GOOGL early-2025 policy keeps it supporting-only. |

This separation is intentional: the generator can produce two disciplined
candidate structures, while the verifier alone attaches a reproducible
verification state.
