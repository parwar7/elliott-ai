# Adaptive Elliott Recount Specification

**Status:** Implemented, staged workflow  
**Policy version:** `adaptive-elliott-recount-policy-3.3.0`  
**Scope:** Technical analysis before the authoritative `resolve_degrees()` freeze

## Purpose

The adaptive recount workflow builds two independent, price-first Elliott
hypotheses, requests only the native timeframe needed for a stated structural
question, and recursively tests proposed parent waves before the normal
technical result is frozen. It extends the existing engine; it does not replace
the legacy `analyze`, `refine`, or `resolve-degrees` paths.

The workflow may return `unproven`, `not_covered`, or `inconsistent`. Missing
data is never evidence against a count, and model prose never creates a
`verified` status.

## Architecture

```text
ProviderCapabilitySet
        |
AdaptiveRecountPlan + resource budgets
        |
AdaptiveTimeframePlanner
        |
DataRequestManifest
        |
Codex/provider acquisition outside the engine
        |
immutable NativeOHLCVBundle + deterministic validation
        |
blind Primary Stage A       blind Alternative Stage A
(typed atomic segmentation) (typed atomic segmentation)
        |                              |
blind Primary Stage B1      blind Alternative Stage B1
(contiguous grouping)       (contiguous grouping)
        |                              |
candidate-scoped evidence   candidate-scoped evidence
        |                              |
blind Primary Stage B2      blind Alternative Stage B2
(group family candidates)   (group family candidates)
        |                              |
deterministic root screen   deterministic root screen
        |                              |
one-degree child generation and subdivision verification
        |
bounded recursive proof trace
        |
status-safe comparison/report
        |
normal ElliottAgent.analyze()
        |
normal ElliottAgent.resolve_degrees()
        |
authoritative stored technical result
```

No Market Scenario or Phase 11 component runs in the pre-freeze loop. The
stored result produced by `resolve_degrees()` remains the immutable downstream
boundary required by `AGENTS.md`.

## Canonical Timeframes

`elliott_ai/timeframes.py` normalizes legacy names and provider aliases while
preserving `1M` (calendar month) versus `1m` (one minute). It supports named
monthly, weekly, and daily bars plus positive integer week, day, hour, and
minute intervals. Examples include `3d`, `12h`, `8h`, `6h`, `4h`, `3h`, `2h`,
`1h`, `45m`, `30m`, `15m`, and `5m`.

`ProviderTimeframeCapability` records:

- canonical interval and duration;
- provider alias;
- higher-timeframe or intraday class;
- native availability and derived status;
- provider, session policy, and adjustment policy.
- market mode, calendar identity and version, timezone, local session bounds,
  session minutes, provider bar alignment, and shortened-terminal-bar policy
  when the adapter can declare them.

`ProviderCapabilitySet` is immutable and content-hashed. The planner may select
only an interval advertised as native. Existing `DEGREE_TIMEFRAMES` values
remain accepted, while `degree_timeframe_compatibility()` applies versioned
granularity bands to additional intervals. Timeframe is evidence resolution,
not a substitute for Elliott degree.

## Adaptive Planner

`AdaptiveTimeframeRequest` binds one structural question to a structural
scoring interval, a separately declared requested-data interval, existing
coverage, provider capability hash, cutoff, and request/bar budgets. The
existing `parent_start_utc` and `parent_end_utc` fields are the structural
interval. `requested_data_start_utc` and `requested_data_end_utc` may extend
earlier only to acquire required context; they never affect timeframe scoring.
The initial `discover_root_structure` request is degree-neutral:
`parent_degree`, `expected_child_degree`, and `parent_timeframe` are null because
the analysis scope is not an Elliott parent. After a root degree is frozen,
ordinary subdivision requests require an assigned parent degree, expected child
degree, and parent timeframe. Supported questions are controlled by
`StructuralQuestion`; free-form model requests cannot acquire data.

`AdaptiveTimeframePlanner.plan()`:

1. verifies the capability hash and advertised intervals;
2. stops before selection when the request budget is exhausted;
3. reuses fully covering native evidence when it already supplies the focused
   bar range;
4. for root discovery, evaluates provider-native, budget-safe intervals using
   only `analysis_start_utc -> analysis_cutoff_utc` and the focused bar target;
5. retains `context_start_utc -> analysis_cutoff_utc` as the root acquisition
   range without letting context length influence the selected timeframe;
6. after root selection, applies the purpose-aware subdivision policy: the
   expected child must be exactly one Elliott degree below the parent, while
   the proof interval must be provider-native and strictly finer than the
   parent evidence interval;
7. avoids the same or near-redundant existing interval;
8. prefers the least expensive view nearest the soft 100-140 bar target;
9. returns a typed `planned`, `already_covered`, `unsupported`, `not_covered`,
   `budget_exhausted`, `configuration_error`, or `unresolved` result.

Timeframe and Elliott degree are independent coordinates. Degree identifies a
wave's structural hierarchy; timeframe identifies the native candles used to
observe or prove it. `root_structure_observation` and
`legacy_degree_resolution` retain the versioned absolute granularity bands for
backward compatibility. `subdivision_proof` and `terminal_refinement` are
relational: `timeframe_compatibility_for_purpose()` requires exact one-degree
descent and a proof interval strictly finer than the parent evidence interval,
but it does not reject the proof merely because the child degree lies outside
an absolute degree/timeframe band. Thus a Primary parent observed on 4h may
have Intermediate children proved on native 1h without relabelling those
children Minor.

Before a non-root request is selected,
`assess_parent_proof_feasibility()` records a content-hashed
`ParentProofFeasibility`. It includes the parent and expected child degrees,
parent evidence timeframe, every provider-supported resolution check,
estimated native bars, viable and rejected intervals, deterministic selection,
provider capability hash, and blockers. Each resolution check requires a
native provider interval, strict granularity descent, requestable complete
coverage, compatible provider/session/adjustment metadata, and the hard bar
budget. Feed identity is bound from the fully covering parent evidence into the
child data manifest and revalidated when the bundle is ingested. The 100-140
bar range remains a ranking preference; a valid finer interval outside that
range remains usable within the hard budget.

If no finer native interval is available, feasibility is `not_covered`. If
finer native intervals exist but the requested degree descent or metadata
policy is contradictory, it is `configuration_error`. If every otherwise
usable finer interval exceeds the hard limit, it is `budget_exhausted`. These
states cannot be collapsed into a false claim that provider data is missing.

### Native-bar estimation

`elliott_ai/adaptive_bar_estimation.py` separates continuous and
session-limited markets. Continuous 24/7 capabilities retain elapsed-time
estimation. Regular- and extended-session equities use their explicitly
declared exchange calendar and provider alignment. Daily estimates count
completed sessions, weekly and monthly estimates count session-containing
calendar buckets, and intraday estimates use the provider's session alignment.
The first XNAS policy removes weekends and rule-calendar holidays, honors
shortened sessions, and records that unexpected exchange closures are not
inferred.

Every `TimeframeCandidateAssessment` records the estimate, basis, calendar ID,
session policy and minutes, provider alignment, quality, assumptions, and exact
structural interval. Estimate quality is one of `exact_calendar`,
`provider_declared`, `session_calendar_estimate`, `coarse_fallback`, or
`unavailable`. Coarse estimates remain visible but receive a deterministic
ranking penalty. No coarse estimate is described as an exact native-bar count.

The Twelve Data adapter declares XNAS regular-session metadata only for an
exchange-qualified NASDAQ/XNAS symbol. It does not assume a 390-minute session
for unknown exchanges or asset classes.

Every request and decision remains in `AdaptiveRecountTrace` with its reason,
assessments, stop condition, and canonical hash.

## Market-Data Boundary

### TradingView MCP audit

The TradingView capability exposed to this Codex installation is:

`mcp__codex_apps__tradingcursor_request_analysis`

It returns a TradingView analysis request/result, not a documented raw-candle
contract. The audited interface does not expose raw OHLCV rows, provider
interval discovery, completed-candle flags, regular-session metadata,
adjustment policy, or pagination. It therefore cannot currently satisfy a
`NativeOHLCVBundle` and is reported as `insufficient_capability`.

The implementation uses the approved Codex-controlled Model B boundary. It
does not invent a Python MCP client and does not label data as TradingView data
unless a real MCP response supplies every required field. Twelve Data remains
a separate supported source; sources are never mixed silently.

### DataRequestManifest

Each planned acquisition is expressed as a hashed `DataRequestManifest` with
the exact symbol, exchange, provider alias, timeframe, structural scoring
interval, requested data interval, structural question, session and adjustment
requirements, cutoff, row budget, and source interface. For root discovery,
`structural_scoring_start_utc` is the requested analysis-window start while
`requested_start_utc` may be the earlier context boundary. Both ranges are
immutable and content-hashed. The requested range must contain the complete
structural range and cannot extend beyond the analysis cutoff.

Subdivision manifests use `adaptive-data-request-manifest-1.4.0`. They bind
the parent bundle ID/hash, parent candidate, selected proof-feasibility
record, parent feed family and parent stream, then declare the expected child
feed family and child stream. A 4h parent therefore requires the same feed
family but a distinct, truthful 1h stream when 1h is selected. Historical
`1.3.0` manifests retain their original exact `required_feed_identity` rule;
they are read but never silently rewritten.

### NativeOHLCVBundle

An accepted bundle records symbol identities, exchange, provider, feed,
session, timezone, price/dividend/volume adjustment, price basis, native and
derived flags, request and actual ranges, acquisition time, cutoff, completed
candle policy, source request ID, candle rows, row hashes, file hash, and
canonical content hash.

New `adaptive-native-ohlcv-bundle-1.1.0` bundles carry two separate immutable
identities:

- `MarketDataFeedFamily` hashes provider, source interface/product, canonical
  and provider symbols, exchange/MIC, session, timezone, price/dividend/volume
  adjustment, and price basis. It deliberately excludes timeframe, provider
  interval alias, and bar duration.
- `MarketDataStreamIdentity` hashes the feed-family hash, canonical timeframe,
  provider interval alias, timestamp semantics, provider bar-alignment policy,
  and native/derived status.

The legacy `feed_identity` remains the provider's truthful interval-specific
label. It is not parsed to manufacture a feed family. A `1.0.0` bundle derives
the strongest possible family and stream from its typed fields in memory, but
serializes and hashes exactly as before. Current child bundles also carry a
`ParentChildDataLineage` linking their manifest, parent bundle, parent
candidate, proof-feasibility record, parent family/stream, and expected child
stream.

Validation rejects:

- derived evidence submitted as native proof;
- incomplete or post-cutoff candles;
- non-finite or invalid OHLCV values;
- duplicate or non-increasing timestamps;
- request, symbol, provider, timeframe, session, adjustment, or hash mismatch;

### Bounded root-timeframe replan

After a valid initial root bundle is ingested, the coordinator counts actual
completed native rows only inside the structural scoring interval. If that
count is at least 15 bars outside the focused range and another non-redundant
native capability improves target distance by at least 15 bars, it appends one
hashed `root_timeframe_replan` request, decision, and manifest. The original
request and decision remain immutable in the trace. Context rows do not enter
the actual structural count. A small mismatch, an invalid bundle, a missing
alternative, an exhausted budget, or a second attempted replan cannot create
another request.
- missing mandatory provenance.

Bundles and traces are written atomically under content-addressed filenames.
Existing files are never overwritten.

## Comparability

`compare_bundle_identity()` applies a purpose-specific deterministic check.
The versioned matrix is:

| Purpose | Required identity |
|---|---|
| Cross-timeframe structural price/subdivision proof | Same feed-family hash and cutoff; child stream must match its own manifest and must differ from the parent stream when timeframe differs. |
| Same-timeframe indicator comparison | Same feed-family hash, canonical timeframe, typed stream hash, legacy provider stream label, and cutoff. |
| Same-timeframe volume comparison | The same strict interval-stream requirements as indicators, including volume adjustment and provider product through the feed family. |
| Cross-timeframe indicator or volume comparison | Always `incomparable` unless a separately versioned methodology is approved. |

Provider, product, symbol, exchange/MIC, session, timezone, adjustments and
price basis remain protected by the feed-family hash. A truthful 1h child is
not rejected merely because its parent is 4h. Conversely, another provider,
product, symbol, MIC, session or adjustment policy cannot pass by presenting
similar prices.

The repository's other exact `feed_identity` checks remain intentionally
strict where they link the same snapshot/fingerprint/outcome, compare
same-timeframe RSI, volume or analogue evidence, or preserve one derived-data
source lineage. Recursive candidate generation and verification use typed
feed-family compatibility instead of exact interval-stream equality. The
complete-history loader retains its historical proof label for artifact
compatibility, but its cross-timeframe compatibility decision uses a typed
feed-family hash.

### Feed-identity equality audit

| Code area | Meaning of equality | Policy |
|---|---|---|
| `adaptive_market_data.validate_bundle_for_manifest` | Child acquisition provenance | Current child manifests compare typed family and expected child stream; legacy `1.3.0` manifests retain exact legacy stream equality. |
| `adaptive_market_data.compare_bundle_identity` | Purpose-specific price, indicator or volume evidence | Feed family for structural price; exact interval stream for same-timeframe indicators/volume; cross-timeframe indicators/volume remain incomparable. |
| `lower_timeframe_candidate_generator`, `lower_timeframe_subdivision_verifier`, `lower_timeframe_recursive_proof`, `session_aligned_derived_4h_proof` | Parent/descendant structural data lineage | Typed feed-family compatibility; timeframe is validated separately at each proof rung. |
| `complete_history_shadow_runner` | Cross-timeframe full-history source family | Typed family hash; the old normalized label is serialization compatibility only. |
| `indicators` | RSI pivot comparison | Exact same-timeframe stream and calculation metadata; unchanged. |
| `fingerprints`, `experience`, `experience_workflow`, `experience_comparison`, `experience_analogue`, `experience_outcomes` | Immutable snapshot/fingerprint/review linkage or same-source analogue comparability | Exact stream identity; unchanged because these are not parent/child timeframe descent. |
| `session_aligned_derived_4h` | Derived artifact back-reference to its exact source rows | Exact source identity plus explicit derived policy/timeframe; unchanged. |
| `forecast_records`, `forecast_outcomes`, `phase11_shadow_workflow`, `market_data`, `correction_state` | Metadata storage, display, or mixed-source disclosure | No cross-timeframe proof decision; existing values remain unchanged. |

Native candles are required for exact pivot catalogs. Aggregated history views
may aid broad interpretation but cannot create an orthodox pivot. Existing
session-aligned `derived_4h` evidence remains explicitly derived and follows
its own approved proof policy; it is never relabelled `native_4h`.

## Analysis Scope and Segmentation

`AdaptiveAnalysisScope` is a non-Elliott container. It stores the requested
window boundary, the as-of observation, cutoff, timeframe, and source hashes.
It has no Elliott degree, family, invalidation, or verification status. The
window start need not be an orthodox pivot or wave origin, and the cutoff need
not be a structural endpoint.

Stage A v4 creates a connected `PriceSegmentationHypothesis` from an ordered
list of steps. The scope start is implicit; each step supplies only the next
`end_boundary_id` and `segment_kind`, and the final step must end at the exact
as-of observation. The parser derives the full boundary path, stable segment
IDs, every non-final `closed` state, and the final `active` state. Descriptor
count and model-authored completion-state mismatches are therefore impossible
in new production output. An `unresolved_interval` may be chronologically
closed when a later boundary exists. The model cannot emit wave labels,
degrees, families, invalidations, indicators, or status. Every segment is
explicitly typed as
`atomic_swing`, `continuation_interval`, `boundary_context`, or
`unresolved_interval`. A closed atomic swing must connect opposite typed
extrema: high to low or low to high. A same-extremum interval must be split or
recorded as continuation/unresolved; it cannot masquerade as one atomic swing.
Every interval from window start to the as-of observation is owned by a
segment, and only the final segment is active.

Every catalog boundary also belongs to one immutable, content-hashed
`PivotChronologyGroup`. Members derived from one native source candle share
one `chronology_ordinal`, source bundle/bar hash, source timestamp, and
timeframe. An ordinary OHLC candle that contributes both a high and low has
`intrabar_order_status=unknown` and `maximum_selectable_members=1`. Its unique
`boundary_ordinal` remains display and serialization metadata only; chronology
uses the group ordinal. The packet exposes these mutually exclusive groups
before generation. A path may select either extremum, but never both, unless a
finer native dataset supplies distinct source timestamps and lineage. The
parser resolves all IDs, rejects a repeated chronology group, and checks
strictly increasing chronology groups before constructing any segment. It
never infers intrabar order from candle color, open/close position, distance,
volume, RSI, or another OHLC-only heuristic.

Unknown same-candle order yields `same_bar_intrabar_order_unknown` with the
group, both members, source hashes/timestamp, and the required relation
`select_at_most_one_boundary_from_chronology_group`. A model cannot repair this
by changing `segment_kind`. The next typed requirement is
`intrabar_order_resolution_required`; compatible finer-native data may create
new, genuinely ordered pivots without retroactively ordering the parent bar.
When finer history is unavailable, the interval remains unresolved.

Historical Stage-A v2 and v3 records retain their original schema versions,
serialized shapes, and hashes. Their readers remain available, including the
exact descriptor-count repair fact for v3. The strict production provider
receives only the v4 step schema.

Stage B1 creates a separate immutable `RootStructuralGroupingHypothesis`.
Each group contains an ordered, non-empty, contiguous set of Stage-A segment
IDs. Each group is internally gap-free. Different groups may overlap only as
alternative proposals in the B1 pool; the coherent set selected as root wave
nodes in B2 must not overlap. Every Stage-A segment outside all proposed groups
must appear in `unresolved_segment_ids`; no interval may be skipped silently.
A group can span several provisional swings and has no Elliott degree,
family, or verification status. Stage B1 is always attempted for both
hypotheses. It is instructed to propose at least one coherent grouping when
meaningful swings exist, unless deterministic parent-level rules leave none;
it may still return no groups rather than force a count.

Stage B2 classifies the frozen groups as candidate Elliott structures and may
assign one provisional degree from the deterministic compatibility set for the
supplied native root timeframe. For example, the active policy permits
`Primary`, `Intermediate`, and `Minor` hypotheses on a daily root view. The set
is evidence-scale policy, not a preselected degree and not verification.
Duration, parent context, relative swing scale, surrounding structure, and
lower-timeframe anatomy may distinguish the surviving degree interpretation.
A root
wave node records the exact `source_group_id`, grouping hash, and ordered
`source_segment_ids`; completed nodes use the group's exact catalog-backed
boundaries. Context and unresolved groups may intentionally have no node. The
result distinguishes `surviving_unproven` from `no_candidate`. Family labels
remain candidate-only and do not become proof merely because a coherent group
was found.

## Blind Candidate Generation

The staged root packets include only:

- the frozen plan and generic rules pack;
- immutable native candle identities;
- price-only rows and deterministic native pivot catalogs;
- cutoff, profile, resource limits, the frozen Stage-A segmentation, and the
  frozen Stage-B1 grouping;
- candidate-scoped technical summaries only after Stage-A boundaries exist.

It excludes prior symbol counts, reports, runs, resolutions, accepted cases,
forecasts, outcomes, lessons, peer output, fundamentals, news, and indicators.
Primary and Alternative calls are made separately with the same evidence. The
Alternative packet cannot see the Primary response. A semantic signature
rejects a graph that differs only through generated IDs or renamed labels;
degree is a material structural distinction, so legitimate Primary and
Alternative counts may choose different allowed root degrees.

Primary and Alternative each receive independent Stage A, Stage B1, and Stage
B2 calls. Peer segmentation, grouping, and classification are never exposed.
Legacy one-call and two-stage root contracts remain readable for old traces,
but the strict production adapter selects the grouped three-stage contract.

The provider schemas prohibit model-authored verification state, confidence,
report prose, and pivots outside the catalog. Live provider calls are disabled
unless the caller explicitly authorizes them.

### Bounded Root-Stage Repair

Stage A, Stage B1, and Stage B2 each permit one initial call and at most two
repair calls per role. A repair is allowed only after strict structured JSON
passes its output schema and deterministic domain parsing rejects it with a
typed repairable contract error. Market-data, cutoff, provenance, capability,
authentication, and Elliott hard-rule failures are never repair prompts.

`RootStageValidationFailure` records the role, stage, attempt, packet and schema
hashes, returned-output hash, semantic signature, controlled error code,
affected IDs, boundary IDs and ordinals, required relation, deterministic
message, repairability, schema-conformance state, UTC time, and content hash.
Repair packets contain only typed deterministic facts, the boundary ordinal
map, exact scope and as-of IDs, and excluded output hashes/signatures. They
contain no prior prose, hidden reasoning, peer output, symbol history, outcome,
or lesson. A semantically repeated malformed result is rejected as
`duplicate_root_stage_candidate` even when generated IDs change.

Primary and Alternative execute as isolated A -> B1 -> B2 workflows. A
contract failure in one role cannot relabel, expose, erase, or cancel a valid
peer role while the global approved call budget remains. One surviving role is
kept under its original role and stays non-authoritative. When both roles
exhaust their contracts, the trace uses `root_candidate_generation_failed` and
`root_stage_contract_exhausted`; `no_valid_candidate` remains reserved for two
successfully parsed hypotheses that yield no surviving market candidate.

Every rejected schema-conforming output is written atomically as a
content-addressed `non_evidence_root_stage_diagnostic`. The artifact contains
only the strict output and typed rejection record. It never becomes price
evidence, an Elliott candidate, or a verification input. Non-schema provider
output is rejected without a repair and is not persisted as structured
diagnostic evidence.

## Deterministic Structural Screen

Each hypothesis is screened independently before deeper work. A direct child
must descend exactly one level through Grand Supercycle, Supercycle, Cycle,
Primary, Intermediate, Minor, Minute, Minuette, and Subminuette. The explicit
reason `same_degree_parent_child_forbidden` rejects same-degree nesting.
This strict descent begins only after B2 has selected a concrete root degree;
the non-Elliott analysis scope never participates in the hierarchy.

For every five-wave motive proposal the screen stores p0-p5, Wave 1/3/5
lengths, Wave 2 origin clearance, Wave 3 extreme clearance, Wave 4/Wave 1
overlap amount, every exact relation, and typed structural invalidations.
Boundary defects such as `boundary_requires_reselection`,
`pivot_not_true_extreme`, and `candidate_segmentation_failed` are kept separate
from Elliott hard-rule invalidations. A failure rejects only that candidate
graph. A surviving graph remains `surviving_unproven`; passing the root screen
is not subdivision proof. An empty result is `no_candidate`, not an unproven
wave and not proof that all Elliott structures are impossible.

Leading diagonals use the repository policy 5-3-5-3-5; ending diagonals use
3-3-3-3-3. Both require five connected waves, Wave 2 origin protection, Wave 3
not shortest, Wave 4/Wave 1 overlap, declared contracting or expanding
geometry, measurable 1/3 and 2/4 boundaries, the corresponding convergence or
divergence, and explicit normal, truncated, or throw-over Wave 5 treatment.
These calculations are versioned and deterministic.

## Recursive Proof

`AdaptiveRecountCoordinator` reuses:

- `LowerTimeframeCandidateGenerator`;
- `LowerTimeframeSubdivisionVerifier`;
- their typed pivots, native windows, policies, and proof statuses.

For one grouped parent and one hypothesis at a time:

1. the planner selects a supported native child timeframe;
2. the corresponding immutable bundle is validated and converted into a
   native window and pivot catalog;
3. one candidate child graph is generated without seeing the peer graph;
4. the deterministic verifier checks family, child count/order, connected
   pivots, containment, hard price rules, invalidations, timeframe consistency,
   hashes, cutoff, and coverage;
5. the result is stored in a new immutable trace version;
6. a rejected child construction may be recounted with different catalog
   pivots, segmentation, child subfamilies permitted by the same frozen parent
   family, completion treatment, or planner-authorized timeframe;
7. unresolved generated children may be selected for the next bounded step.

A same-parent reconstruction is family-locked. It can rebuild the internal
anatomy of the frozen parent, but it cannot turn an impulse into a diagonal or
one corrective family into another. A different parent family requires a new
immutable parent candidate with its own ID and semantic signature. Parent
reclassification is intentionally deferred from recursive reconstruction; a
cross-family proposal yields `parent_family_reclassification_required` and
leaves the original parent `unproven`.

One parent may retain an initial graph and at most two additional immutable,
hash-distinct reconstruction attempts (`MAX_CHILD_RECOUNTS_PER_PARENT = 3`).
A rejected child graph yields `recount_required`; it does not make the parent
inconsistent. Under the active versioned capability,
`family_exhaustive_impossibility_supported` is `false`: only a direct
deterministic hard-rule failure in the frozen completed parent and its already
frozen direct children can prove structural inconsistency. Failed generated
graphs and three exhausted model attempts never eliminate the full family or
segmentation space. Exhausting the bounded search yields `unproven` with
`candidate_search_exhausted`. Missing compatible history yields `not_covered`.

The same source-candle chronology rule applies to generated child graphs,
reconstruction exclusions, parent/child boundary chains, and subdivision
verification. Candidate generation rejects an over-selected group with
`intrabar_order_resolution_required`. The verifier independently emits
`intrabar_pivot_order_unresolved` and normally keeps the graph `unproven` with
`lower_timeframe_resolution_required`; unknown OHLC chronology is not proof
that the frozen Elliott parent is structurally inconsistent.

The only proof statuses are `verified`, `unproven`, `not_covered`, and
`inconsistent`. Verification propagates from children to parents. An active
parent cannot be verified as completed. Missing compatible coverage produces
`not_covered`; exhausted depth, request, graph, pivot, node, or bar budgets can
never produce `verified`.

## Profiles and Indicators

The legacy low-level feature defaults are unchanged. The named `elliott_full`
profile enables EWO, MACD, volume, volatility/scale diagnostics, and optional
Wilder RSI.

Stage-A price boundaries and Stage-B1 groups are established before indicator
audit. Stage B2 may use candidate-scoped summaries to rank classifications,
and every summary is
bound to its segment, timeframe, source bundle, and hash.
RSI, volume, EWO, MACD, Fibonacci, channels, duration, and volatility may be
`supporting`, `neutral`, `contradicting`, `unavailable`, or `incomparable`.
They cannot create a pivot, select a family, or hard-invalidate an otherwise
valid count. Endpoint comparisons and within-wave extrema are kept distinct.

If a summary was not calculated, it remains unavailable. Indicator evidence is
never passed into deterministic hard-rule calculations. Final resolution
analytics continue to use the existing canonical indicator and wave-metrics
pipeline; no values are fabricated in the trace.

## RKLB Strict Blind Plan

`rklb_strict_blind_plan()` fixes only the requested symbol, analysis-window
start (`2026-05-27`), context boundary, cutoff, profile, and budgets. It does
not hard-code an Elliott origin, pivot, count, target, or invalidation. Price
before the requested window may provide parent context but cannot silently
become part of the requested local structure. Root timeframe scoring uses the
`2026-05-27 -> cutoff` structural interval; the earlier context boundary remains
in the acquisition manifest only.

## Readiness and Freeze

`mark_ready_for_final_comparison()` succeeds only after each surviving branch
has explicit deterministic proof or a bounded unresolved/not-covered stop.
It never changes a candidate in place.

`ElliottAgent.finalize_adaptive_recount()` requires a hash-valid ready trace,
runs the normal strict final comparison, and then calls the existing
`resolve_degrees()` path. Only the returned stored degree-resolution ID marks
the trace `frozen_authoritative`. The method does not activate Phase 11.

## CLI

The staged interface is:

```text
python -m elliott_ai adaptive-recount mcp-status
python -m elliott_ai adaptive-recount plan ...
python -m elliott_ai adaptive-recount status --trace TRACE.json
python -m elliott_ai adaptive-recount ingest --trace TRACE.json --bundle BUNDLE.json
python -m elliott_ai adaptive-recount generate-roots ... --allow-model-call
python -m elliott_ai adaptive-recount plan-proof ...
python -m elliott_ai adaptive-recount prove ... --allow-model-call
python -m elliott_ai adaptive-recount ready --trace TRACE.json
python -m elliott_ai adaptive-recount report --trace TRACE.json --output REPORT.md
python -m elliott_ai adaptive-recount finalize ... --allow-model-call --commit
```

All commands before `finalize` are file-only and are dispatched before the
SQLite store is opened. `finalize` is the sole adaptive command authorized to
use normal analysis and degree-resolution persistence, and it requires both
explicit model-call authorization and `--commit`.

## Human Report Contract

The Markdown report separates the non-Elliott scope, segment map, Primary,
Alternative, and their exact discriminators. Each candidate shows anchors,
degree, family, completion state, timeframe, deterministic proof, active wave,
candidate boundary conditions, reconstruction attempts, unresolved evidence,
and honest indicator availability. It never presents a percentage probability
and never describes an unproven model label as verified.

The report labels four failure classes separately: `NO MARKET CANDIDATE`,
`ROOT GENERATION CONTRACT FAILURE`, `CANDIDATE HARD-RULE REJECTION`, and
`MISSING DATA / NOT COVERED`. It states whether Primary and Alternative were
actually called. It never says that no second candidate existed when the
Alternative workflow was not called or failed its generation contract.

## Persistence and Compatibility

The initial adaptive implementation adds no SQLite table or migration. Plans,
requests, bundles, candidates, proof records, and traces are canonical JSON
artifacts. Existing stored runs and public commands remain readable. Normal
database writes occur only at the existing final analysis/resolution boundary.
Legacy provider capabilities, planner requests, decisions, assessments, and
manifests serialize with their original schema shape so preserved trace hashes
remain valid.

Root-stage failure and diagnostic references are append-only fields on new JSON
traces. They add no SQLite table or migration. When those fields are absent,
legacy trace serialization omits them and preserves the original content hash.

On Windows, `ZoneInfo` requires the IANA database pinned in `requirements.txt`.
The bundled Codex runtime already supplies the same `tzdata` version.

## Safety Stops

Stop without improvising when:

- provider capabilities are empty, unknown, or incompatible;
- a required MCP tool cannot return raw provenance-complete OHLCV;
- a bundle or trace hash fails;
- candle completion or cutoff cannot be proven;
- Primary and Alternative are semantic duplicates;
- a model invents a pivot or verification field;
- proof coverage is missing or resource budgets are exhausted;
- neither candidate survives deterministic price rules.

## Current Limitation

The present TradingView MCP surface cannot deliver the raw candles required by
the bundle contract. The planner, manifest, immutable ingestion, validation,
recursive proof, reporting, and final-freeze path are implemented, but a real
TradingView-backed acceptance run must wait for a documented raw-OHLCV MCP
operation. This limitation is explicit and does not fall back to scraping or
fabricated data.
