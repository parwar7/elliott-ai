# Phase 5 Historical Pattern Library Completion Report

**Completed:** 30 July 2026  
**Scope:** Offline historical pattern candidates, two-pass extraction, and
immutable pattern-library construction

## Implementation Summary

Phase 5 adds a standalone framework that turns multiple finalized historical
evidence packets and causal analyses into deterministic grouping proposals,
bounded LLM-synthesized pattern hypotheses, adversarially reviewed versions,
and immutable in-memory pattern libraries.

The implementation preserves four separate layers:

1. factual, dated `HistoricalEvidencePacket` records;
2. bounded single-case `HistoricalCausalAnalysis` interpretations;
3. deterministic `PatternCandidate` grouping proposals; and
4. reusable but explicitly non-universal `HistoricalPattern` hypotheses.

It is not integrated with Elliott analysis, the active Market Scenario
pipeline, current-market reasoning, CLI, reporting, persistence, research,
retrieval, prediction, probability, or trade decisions.

## Files

Created:

- `elliott_ai/historical_patterns.py`
- `elliott_ai/historical_pattern_extractor.py`
- `scripts/test_historical_patterns.py`
- `scripts/test_historical_pattern_extractor.py`
- `HISTORICAL_PATTERN_PHASE5_COMPLETION_REPORT.md`

Modified:

- `docs/market_scenario_engine_spec.md`

No Elliott, active-agent, provider, prompt used by technical analysis, CLI,
reporting, or database file was changed. Migration count is zero.

## Contracts

Immutable, canonical-JSON contracts were added for:

- `PatternOutcomeProfile`;
- `PatternSupportMetrics`;
- `PatternCandidate`;
- `HistoricalPatternCandidateBuildResult`;
- `HistoricalPattern`;
- `PatternOverlapDiagnostic`;
- `HistoricalPatternLibrary`;
- `HistoricalPatternLibraryBuildResult`;
- `HistoricalPatternAdversarialReview`; and
- `HistoricalPatternExtractionResult`.

Mappings are frozen, collection fields are tuples, timestamps normalize to
UTC, controlled values use enums, and every persisted-shaped result has a
canonical SHA-256 content hash.

## Final Enums

`PatternStatus`:

- `draft`
- `extracted`
- `reviewed`
- `validated`
- `rejected`
- `deprecated`
- `insufficient_support`

`PatternDirection`:

- `bullish`
- `bearish`
- `bidirectional`
- `context_dependent`

`PatternConfidence`:

- `high`
- `moderate`
- `low`
- `insufficient_evidence`
- `unavailable`

Additional constrained vocabularies:

- `PatternChange`: `expanding`, `contracting`, `stable`, `mixed`, `unknown`,
  `unavailable`
- `PatternTendency`: `strong`, `moderate`, `weak`, `context_dependent`,
  `unknown`, `unavailable`
- `PatternRecoveryProfile`: `rapid`, `gradual`, `partial`, `full`, `none`,
  `mixed`, `unknown`, `unavailable`
- `PatternConsistency`: `high`, `moderate`, `low`, `mixed`,
  `insufficient_evidence`, `unavailable`
- `EvidenceIndependenceStatus`: `independent`, `mixed`, `correlated`,
  `unknown`, `insufficient_evidence`
- `PatternOverlapRelationship`: `none`, `overlap`, `near_duplicate`,
  `parent_child`, `mutually_contradictory`, `regime_dependent_outcomes`
- `PatternAdversarialRecommendation`: `keep`, `revise`, `reject`,
  `insufficient_support`

Confidence values remain qualitative evidence descriptions. No calibrated
probability was added.

## Schema and Prompt Versions

- Candidate: `historical-pattern-candidate-1.0.0`
- Pattern: `historical-pattern-1.0.0`
- Library: `historical-pattern-library-1.0.0`
- Candidate/library build result:
  `historical-pattern-build-result-1.0.0`
- Validation: `historical-pattern-validation-1.0.0`
- Extraction result: `historical-pattern-extraction-result-1.0.0`
- Adversarial review: `historical-pattern-adversarial-review-1.0.0`
- Synthesis prompt: `historical-pattern-prompt-1.0.0`
- Adversarial prompt: `historical-pattern-adversarial-prompt-1.0.0`

Existing historical-case, causal-analysis, similarity-feature, Market
Scenario, Elliott, and database schema versions were unchanged.

## Deterministic Candidate Generation

`HistoricalPatternCandidateBuilder`:

1. verifies input types;
2. rejects duplicate packet, causal-analysis, and similarity-feature case IDs;
3. requires packet and analysis case sets to match exactly;
4. freshly validates every packet, analysis, feature, and content hash;
5. joins all sources by exact case ID;
6. identifies materially duplicated moves from symbol, exchange, dates,
   endpoints, and prices;
7. retains the lexicographically first duplicate and reports the others;
8. creates controlled grouping tokens;
9. groups by exact set membership;
10. consolidates duplicate case groups while preserving all grouping bases;
11. rejects groups below the configurable minimum; and
12. finalizes candidates in stable ID order.

Grouping tokens may use move type, exposure type, transmission mechanism,
normalized event type, market-regime feature, magnitude bucket, duration
bucket, and technical structure.

The default minimum is three cases. It cannot be configured below two, and a
one-case pattern is never produced.

Candidate IDs derive from canonical case membership. Shared features are
recomputed from the source intersections, while differing features remain
visible.

## Independence and Concentration

Support metrics record:

- supporting, contradicting, and exception counts;
- unique symbols, sectors, regimes, and move types;
- independent source groups;
- earliest case, latest case, and temporal span;
- outcome and mechanism consistency;
- regime diversity;
- symbol and sector concentration; and
- evidence-independence status.

Independent source groups are connected components of overlapping source
hashes. Direct or transitive source overlap forms one group. This prevents
partially overlapping evidence sets from appearing independent.

The builder warns for:

- one-symbol concentration;
- one-sector concentration;
- unavailable sector identity;
- overlapping source groups;
- shared parent events;
- insufficient regime diversity;
- insufficient temporal diversity;
- duplicate historical moves; and
- no available negative examples.

Concentration does not automatically invalidate every specialist pattern.
Full symbol or sector concentration must be disclosed and paired with low,
insufficient, or unavailable confidence. Correlated evidence likewise blocks
moderate or high confidence. High confidence requires multiple symbols,
multiple regimes, multiple independent source groups, and independent
evidence.

## Pattern Extractor Boundary

`HistoricalPatternExtractor` accepts one finalized candidate plus exactly its
frozen packets and causal analyses. It checks:

- exact case membership;
- no duplicate sources;
- finalized source validation;
- causal-analysis hash equality;
- configured minimum support;
- source cutoff ordering; and
- prompt-size bounds.

The prompt asks:

> What recurring combination of initiating conditions, exposures, triggers,
> transmission mechanisms, amplifiers, dampeners, and market regimes is
> supported across these cases, and under what conditions does it fail?

The model controls semantic hypothesis fields only. The application controls
pattern ID, version, status, metrics, source hashes, model provenance, prompt
version, timestamps, cutoff, validation, and content hash.

The prompt requires invariant, common, optional, and disqualifying features;
separate causal lanes; complete case accounting; explicit contradictions and
exceptions; limitations; failure conditions; missing evidence; conditional
language; and the smallest defensible pattern.

It forbids invented cases, evidence, events, exposures, mechanisms, regimes,
sources, or outcomes. It also forbids guarantees, universal causal laws,
statistical claims, and outcome-defined or tautological patterns.

The extractor prefers `generate_strict_json()` when available and otherwise
uses the existing provider contract. OpenAI, Ollama, and deterministic
`fixture` providers are supported. Packet mode returns a structured
no-reasoning result. No research or web tool is available to either pass.

## Adversarial Review

The second pass uses the exact same frozen candidate and historical sources.
It tests:

- overgeneralization;
- hindsight, survivorship, and selection bias;
- symbol and sector concentration;
- regime dependence;
- duplicate and correlated evidence;
- minimized contradictions;
- falsifiability;
- outcome leakage;
- tautology and circularity;
- causal-lane confusion;
- correlation presented as causation;
- support sufficiency; and
- missing invalidation conditions.

Every review must reference all candidate cases exactly once and populate each
assessment.

Outcomes:

- `keep`: returns a reviewed version of the original pattern.
- `revise`: requires a complete valid replacement with the same stable
  pattern ID and exactly the next version.
- `reject`: returns an explicit unsuccessful result with rejected status.
- `insufficient_support`: returns an explicit unsuccessful result with
  insufficient-support status.

The adversarial review stores the original draft hash. A revision never
silently mutates that draft.

## Falsifiability Validation

Every valid pattern requires:

- stable identity, version, name, and description;
- minimum case support;
- disjoint supporting, contradicting, and exception roles;
- exact candidate accounting and source-analysis hashes;
- invariant and disqualifying features;
- initiating conditions;
- at least one controlled transmission mechanism;
- a trigger or explicit no-single-trigger state;
- a source-consistent outcome profile;
- limitations and missing evidence;
- incompatible regimes or failure conditions; and
- an explanation when no contradicting case is available.

Validation rejects:

- dangling case or analysis references;
- source-hash mismatch;
- controlled values absent from sources;
- overlapping case roles;
- inconsistent metrics;
- cutoff leakage;
- unsupported certainty or statistical claims;
- universal causal-law language;
- circular or outcome-defined statements;
- confidence that ignores concentration; and
- canonical-hash mismatch.

Ordinary analytical invalidity returns `ValidationResult`; public API type
errors remain programmer errors.

## Identity, Revision, and Deprecation

`pattern_id` is stable for one conceptual pattern.
`pattern_version` is a positive monotonically increasing integer.

A revised pattern creates a new version. Existing versions remain immutable.
Library updates reject versions that do not advance beyond the highest
existing version.

Deprecated versions use `PatternStatus.DEPRECATED` and may identify a valid
replacement through `replacement_pattern_ref`. Self-reference, dangling
replacement references, and duplicate ID/version pairs are invalid.

## Pattern Library

`HistoricalPatternLibraryBuilder`:

- accepts only reviewed, validated, or explicitly deprecated patterns;
- rejects draft, merely extracted, rejected, and insufficient-support records;
- freshly validates every pattern and hash;
- preserves existing versions;
- enforces monotonically advancing new versions;
- orders by stable pattern ID and version;
- rejects duplicate references;
- validates replacement and deprecation references;
- requires exact source-case and source-analysis hash coverage;
- rejects conflicting hashes for one case;
- computes deterministic overlap diagnostics; and
- returns a typed, content-hashed build result.

No database or file persistence was added.

## Overlap Diagnostics

Every pattern pair is compared using:

- exposure mechanisms;
- transmission mechanisms;
- event types;
- required and optional regimes;
- supporting case IDs; and
- the full outcome profile.

Diagnostics identify overlap, near duplicates, parent-child relationships,
mutually contradictory patterns, and regime-dependent outcomes.

Raw shared dimensions remain visible. The implementation never merges,
deletes, promotes, or rewrites patterns automatically.

## Retry Policy

The default is two attempts per pass, configurable from one to three.

Retried:

- malformed JSON;
- missing, unknown, or mistyped fields;
- invalid enums;
- dangling references;
- invented controlled values;
- omitted limitations;
- prohibited certainty;
- circular wording;
- incomplete adversarial review;
- invalid revision output; and
- other model-correctable deterministic failures.

Not retried:

- invalid or unfinalized input;
- support below the configured minimum;
- concentration or source dependence requiring new cases;
- genuine evidence insufficiency;
- packet-only or unsupported providers;
- credential, HTTP, network, or other infrastructure failures; and
- prompt-size failure.

Correction prompts contain only deterministic rule feedback and add no facts.

## Offline Test Coverage

The fixture provider is deterministic and queue-backed. It records detached
prompt, schema, and packet copies and returns predefined objects or errors.

Tests cover:

- bullish impulse and bearish crash patterns;
- no-single-trigger and regime-lane patterns;
- contradicting and exception cases;
- all four adversarial recommendations;
- revision version advancement;
- malformed JSON retry;
- dangling cases;
- invented exposure and transmission values;
- unsupported certainty;
- missing limitations;
- circular patterns;
- concentration no-retry behavior;
- genuine insufficiency;
- provider and packet-mode boundaries;
- application-owned provenance;
- prompt bounds;
- source immutability;
- stable hashes;
- JSON round trips;
- repeat-run determinism;
- one-symbol and one-sector concentration;
- duplicate historical moves;
- connected source-overlap groups;
- minimum support;
- stable pattern versioning;
- deprecation without overwrite;
- unreviewed library exclusion;
- deterministic library ordering; and
- deterministic overlap diagnostics without merging.

No test requires OpenAI, Ollama, credentials, network, web research, or live
market data.

## Verification

Focused command:

```powershell
python -m unittest scripts.test_historical_patterns scripts.test_historical_pattern_extractor
```

Result:

```text
Ran 58 tests in 3.221s
OK
```

Complete repository command:

```powershell
& 'C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s scripts -p 'test_*.py'
```

Result:

```text
Ran 578 tests in 40.659s
OK
```

Exact totals:

- passed: 578
- failed: 0
- errors: 0
- skipped: 0

The complete suite includes existing SQLite integrity and foreign-key tests.
No database schema or database record was modified by Phase 5.

## Deferred Work

Explicitly deferred:

- historical case research and extraction;
- live web, news, filings, fundamentals, events, and market-data access;
- persistent pattern storage;
- database migrations;
- CLI and reporting integration;
- pattern-library retrieval;
- embeddings and vector search;
- learned or adaptive similarity;
- current-market pattern matching;
- current-market scenario generation from patterns;
- probabilities, statistical calibration, prediction, or analogue voting;
- active Elliott or Market Scenario pipeline integration;
- automatic wave resolution; and
- trade decisions or execution.

Any future retrieval integration must be separately specified and must retain
source cutoffs, versions, contradiction evidence, concentration warnings,
deterministic hashes, and the non-predictive boundary.
