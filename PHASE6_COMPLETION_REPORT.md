# Phase 6 Completion Report

## Status

Phase 6, Current State Builder and Deterministic Pattern Retrieval, is
complete.

The implementation is standalone and offline. It is not connected to the
active Elliott pipeline, Market Scenario reporting, CLI, providers, live
research, web access, persistence, scenario generation, or trade logic.

## 1. Implementation Summary

Phase 6 adds two bounded services:

1. `CurrentStateBuilder` freezes an existing technical premise and already
   supplied current evidence, exposures, category coverage, events, regime,
   and optional normalized context into one immutable retrieval state.
2. `PatternRetrievalEngine` compares that state with eligible immutable
   Phase 5 historical patterns through one versioned deterministic scoring
   profile.

The implementation preserves separate:

- frozen technical input;
- current evidence and exposures;
- normalized current state;
- historical pattern;
- primary pattern match;
- counter-pattern match; and
- retrieval execution result.

It does not mutate or regenerate any source layer.

## 2. Files

Created:

- `elliott_ai/current_state.py`
- `elliott_ai/pattern_retrieval.py`
- `scripts/test_current_state.py`
- `scripts/test_pattern_retrieval.py`
- `PHASE6_COMPLETION_REPORT.md`

Modified:

- `docs/market_scenario_engine_spec.md`

No Elliott, provider, prompt, CLI, report, database, or Phase 3-5 source file
was modified.

## 3. Current-State Enums

`TechnicalDirection`:

- `bullish`
- `bearish`
- `neutral`
- `mixed`
- `unavailable`

`FeatureAvailability`:

- `available`
- `researched_no_signal`
- `unavailable`
- `assumed`
- `draft`

## 4. Retrieval Enums

`MatchQuality`:

- `very_strong`
- `strong`
- `moderate`
- `weak`
- `very_weak`
- `insufficient_data`

`MatchRecommendation`:

- `include`
- `include_with_warning`
- `exclude`
- `insufficient_data`

`RetrievalLane`:

- `primary`
- `counter`

The module also defines controlled `MatchExplanationCode` and
`PatternExclusionCode` values. Core reasoning therefore uses codes rather
than generated prose.

## 5. CurrentStateBuilder

Inputs:

- one immutable `FrozenTechnicalPremise`;
- current `EvidenceItem` records;
- current `ExposureItem` records;
- exposure-category coverage;
- optional `MaterialEvent` records;
- optional `MarketRegime`; and
- optional normalized valuation, liquidity, positioning,
  benchmark-relative, and sector-relative states.

The builder:

- validates all input types and references;
- preserves the frozen technical content hash;
- calculates canonical evidence, exposure, and event packet hashes;
- preserves source IDs and source hashes;
- normalizes retrieval features without changing technical interpretation;
- records exact availability states;
- separates assumptions from confirmed facts;
- derives only explicitly supported transmission hypotheses;
- deep-freezes mappings and sequences;
- calculates a canonical state hash;
- stabilizes deterministic validation; and
- returns `CurrentStateBuildResult`.

The state supports both full source-reconciliation validation and standalone
validation. In standalone mode, frozen exposure and event IDs are used to
verify stored transmission references.

## 6. Technical Normalization

Direction maps directly from `ScenarioDirection`.

Move type, structure, magnitude, and duration are read only from explicit
controlled technical fields, including optional `retrieval_features` inside
the frozen premise.

Unavailable or unmappable values remain unavailable. The builder does not
infer:

- crash from target size;
- impulse from target direction;
- squeeze from magnitude;
- reversal from confidence;
- duration from wave degree; or
- any new Elliott interpretation.

## 7. Transmission Mapping

The versioned deterministic exposure mapping is:

| Exposure | Transmission |
|---|---|
| `valuation_compression` | `multiple_compression` |
| `valuation_expansion` | `multiple_expansion` |
| `dilution_risk` | `dilution_expectation` |
| `financing_risk` | `financing_fear` |
| `short_interest_risk` | `short_covering` |
| `momentum_unwind_risk` | `momentum_unwind` |
| `execution_risk` | `execution_repricing` |
| `regulatory_risk` | `regulatory_repricing` |
| `sector_demand_risk` | `demand_repricing` |
| `crowded_trade_risk` | `crowded_positioning_unwind` |

Exact event mappings support:

- `earnings_revision`;
- `guidance_revision`;
- `regulatory_repricing`; and
- explicitly prefixed controlled transmission mechanisms.

Every mechanism retains supporting exposure or event IDs. Unmapped exposures
produce warnings and no forced mechanism.

## 8. Scoring Profile

Profile:

`pattern-retrieval-scoring-profile-1.0.0`

Weights:

| Component | Maximum points |
|---|---:|
| Exposure | 30 |
| Transmission | 20 |
| Market regime | 15 |
| Events | 10 |
| Technical | 15 |
| Outcome compatibility | 10 |
| Total | 100 |

The formula is:

```text
clamp(
    six component contributions
    + support quality adjustment
    - concentration penalty
    - missing-data penalty
    - incompatibility penalty
    - contradiction penalty
    - limitation penalty,
    0,
    100
)
```

Default penalties:

- researched missing required feature: 6;
- unavailable required feature: 3;
- incompatible regime: 12;
- other contradiction or incompatibility: 7;
- one-symbol concentration: 4;
- one-sector concentration: 3;
- correlated sources: 5;
- mixed sources: 1;
- unknown independence: 2;
- insufficient independence evidence: 3;
- high missing-data share: up to 10; and
- controlled applicable limitation: 4.

Support adjustment:

- independent and regime-diverse: +2;
- independent: +1;
- mixed: +0.5; and
- correlated, unknown, or insufficient: +0.

Raw supporting-case count never adds points.

## 9. Matching Semantics

Required conditions have the largest effect. They include controlled
initiating exposures, transmissions, required regimes, required events,
direction, applicable move-type compatibility, and explicit invariant
technical structures.

Optional conditions include amplifying exposures, optional regimes,
no-single-trigger event context, controlled common features, and comparable
outcome-profile fields.

Incompatible conditions are explicit incompatible regimes and controlled
disqualifying features.

Contradictions are known opposing current values, including a required regime
field with a different known state, opposing direction, opposing move type,
or a present dampening exposure.

Unknown and missing are distinct:

- `researched_no_signal` is a known missing condition;
- `unavailable` and `assumed` are unknown;
- a present deterministic `draft` exposure is matchable and remains labelled
  draft in the source state.

Unknown values reduce information completeness and do not become
contradictions.

Generic historical-pattern prose is never semantically interpreted. Only
exact controlled feature tokens participate in retrieval.

## 10. Primary and Counter Lanes

Aligned bullish and bearish patterns enter the primary lane.

An opposite-direction pattern may enter the counter lane only when it shares
a meaningful exposure, transmission, regime, event, move type, or exact
technical structure. Its direction mismatch remains visible and penalized.

Primary and counter lanes have:

- separate lists;
- separate limits;
- separate contiguous ranks; and
- no score or ordering contamination.

Context-dependent patterns require explicit query permission plus a matching
regime or event condition. Bidirectional patterns also require explicit query
permission.

## 11. Match Quality

Quality thresholds:

| Quality | Minimum score | Minimum completeness |
|---|---:|---:|
| `very_strong` | 85 | 0.85 |
| `strong` | 70 | 0.70 |
| `moderate` | 55 | 0.50 |
| `weak` | 40 | 0.35 |
| `very_weak` | 0 | 0.35 |
| `insufficient_data` | n/a | below 0.35 |

`very_strong` is additionally blocked when aggregate penalties exceed 3.

Deterministic ordering:

1. descending total score;
2. descending quality;
3. descending completeness;
4. ascending pattern ID; and
5. descending pattern version.

## 12. Overlap Diversity

The engine reuses the Phase 5 `analyze_pattern_overlaps()` diagnostics.

Within each lane, connected `near_duplicate` patterns form a cluster. The
default profile retains one representative under normal retrieval ordering.
Suppressed members remain visible through:

- `suppressed_pattern_refs`;
- exclusion reason
  `overlap_near_duplicate_suppressed`; and
- raw overlap diagnostics.

Patterns are never merged, deleted, rewritten, or repaired.

Other overlap relationships do not trigger suppression in this version.

## 13. Cutoff Protection

Current evidence follows:

```text
publication <= item applicable cutoff <= current state cutoff
```

Future events are accepted only as explicitly scheduled calendar context.
Regime observations and the frozen technical market-data cutoff cannot exceed
the current-state cutoff.

Historical patterns must:

- pass their own Phase 5 cutoff validation;
- retain valid source references and hashes; and
- have `pattern.applicable_cutoff <= current_state.applicable_cutoff`.

The retriever performs no live or post-cutoff research.

## 14. Schema Versions

- `current-market-state-1.0.0`
- `current-market-state-build-result-1.0.0`
- `current-market-state-validation-1.0.0`
- `current-state-builder-1.0.0`
- `current-technical-normalization-1.0.0`
- `current-transmission-policy-1.0.0`
- `pattern-retrieval-query-1.0.0`
- `retrieved-pattern-match-1.0.0`
- `pattern-retrieval-result-1.0.0`
- `pattern-retrieval-execution-result-1.0.0`
- `pattern-retrieval-validation-1.0.0`
- `pattern-retrieval-scoring-profile-1.0.0`
- `pattern-retrieval-engine-1.0.0`

No existing Phase 3, Phase 4, Phase 5, Elliott, market-scenario, CLI, report,
or database schema version changed.

## 15. Validation

Deterministic validation covers:

- state identities, hashes, references, availability, assumptions,
  transmissions, provenance, and cutoffs;
- query IDs, hashes, limits, quality, status, profile, and immutable input
  binding;
- match identity, hash, rank, score ranges, score reconciliation, quality,
  recommendation, explanation uniqueness, required-feature accounting,
  disjoint feature states, and pattern reference;
- result query validity, match validity, lane assignment, rank continuity,
  ordering, uniqueness, lane separation, limits, complete library accounting,
  eligibility, exclusions, overlap references, profile identity, and result
  hash.

Ordinary invalid input returns a structured unsuccessful result. Programmer
type errors may raise.

## 16. Offline Tests

New focused coverage:

- 17 current-state builder and contract tests;
- 29 retrieval, eligibility, scoring, overlap, validation, serialization,
  determinism, and immutability tests;
- 46 total Phase 6 focused tests.

Coverage includes all requested bullish, bearish, partial, unavailable,
cutoff, scheduled-event, exposure, transmission, regime, incompatible,
technical, primary, counter, context-dependent, missing, unknown,
concentration, independence, overlap, deprecated, rejected, invalid-hash,
ranking, tie-break, reconciliation, threshold, round-trip, and compatibility
cases.

No test uses OpenAI, Ollama, web access, a database, credentials, or live
market data.

## 17. Test Results

Focused:

```powershell
& 'C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest scripts.test_current_state scripts.test_pattern_retrieval
```

Result:

```text
Ran 46 tests in 2.142s
OK
```

Full repository:

```powershell
& 'C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s scripts -p 'test_*.py'
```

Result:

```text
Ran 624 tests in 40.228s
OK
```

Exact totals:

- passed: 624;
- failures: 0;
- errors: 0.

An initial run with the system Python executable reached 614 tests but could
not import the pre-existing pandas-dependent
`test_elliott_analysis_layers` module because that interpreter does not have
`pandas`. The canonical bundled dependency runtime above includes the
repository dependencies and completed all 624 tests.

## 18. Deferred Work

Deferred to separately approved future phases:

- active Elliott or normal analysis-pipeline integration;
- current scenario or narrative generation;
- LLM reasoning;
- live web, news, filing, fundamental, event, or market-data research;
- embeddings, vector search, learned similarity, or adaptive weights;
- probability or confidence calibration;
- analogue voting;
- automatic Elliott Wave resolution;
- trade or investment recommendations;
- database persistence or migration;
- CLI commands;
- report and presentation integration; and
- provider changes.

Phase 6 retrieval scores are deterministic compatibility diagnostics. They are
not probabilities, forecasts, expected returns, or evidence that a historical
outcome will recur.
