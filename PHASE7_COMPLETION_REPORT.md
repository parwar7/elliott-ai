# Phase 7 Completion Report

## Status

Phase 7, Current Market Scenario Generator LLM, is complete.

The generator is standalone. It is not connected to the active Elliott
pipeline, live research, web access, persistence, CLI, reports, trading, or
automatic technical-count modification.

## 1. Implementation Summary

Phase 7 adds:

1. immutable current-scenario contracts;
2. deterministic input, narrative, relationship, diversity, adversarial, and
   execution-envelope validation;
3. one versioned strict-JSON synthesis prompt;
4. one versioned strict-JSON adversarial prompt;
5. a bounded two-pass provider service;
6. structured insufficient-evidence and provider-failure results;
7. canonical SHA-256 hashes and application-owned provenance; and
8. 51 offline fixture tests.

The model answers what combinations of already supplied current conditions,
plausible future conditions, exposure interactions, market regimes, and
retrieved historical causal structures could conditionally produce the
frozen technical move. It does not decide whether the technical premise is
correct.

## 2. Files

Created:

- `elliott_ai/current_scenarios.py`
- `elliott_ai/current_scenario_generator.py`
- `scripts/test_current_scenario_generator.py`
- `PHASE7_COMPLETION_REPORT.md`

Modified:

- `docs/market_scenario_engine_spec.md`

Not modified:

- Elliott calculations or validators;
- `elliott_ai/agent.py`;
- `elliott_ai/cli.py`;
- `elliott_ai/providers.py`;
- `elliott_ai/reporting.py`;
- active prompts;
- database code or schemas; and
- existing Phase 3-6 contracts or logic.

## 3. Controlled Enums

`ScenarioType`:

- `company_execution`
- `earnings_and_guidance`
- `financing_and_dilution`
- `valuation_repricing`
- `macro_and_liquidity`
- `sector_rotation`
- `regulatory`
- `event_driven`
- `positioning_unwind`
- `short_squeeze`
- `multi_factor`
- `technical_invalidation`
- `insufficient_evidence`

`TechnicalPremiseCompatibility`:

- `strongly_consistent`
- `consistent`
- `partially_consistent`
- `weakly_consistent`
- `contradictory`
- `insufficient_evidence`

`ScenarioConfidence`:

- `high`
- `moderate`
- `low`
- `insufficient_evidence`
- `unavailable`

`ScenarioStepType`:

- `precondition`
- `trigger`
- `initial_repricing`
- `amplification`
- `continuation`
- `exhaustion`
- `reversal`
- `invalidation`

`ScenarioRelationshipType`:

- `mutually_exclusive`
- `compatible`
- `prerequisite`
- `sequential`
- `alternative_trigger`
- `amplification_of`
- `invalidates`
- `counter_scenario`

`ScenarioAdversarialRecommendation`:

- `keep`
- `revise`
- `reject`
- `insufficient_evidence`

Existing controlled `ScenarioDirection`, `ExposureType`,
`TransmissionMechanism`, `MagnitudeBucket`, `DurationBucket`,
`RetrievalLane`, `MatchQuality`, and `MatchRecommendation` enums are reused.

## 4. Immutable Input Boundary

`CurrentMarketScenarioGenerator.generate()` accepts:

- one finalized `FrozenTechnicalPremise`;
- one finalized `CurrentMarketState`;
- one finalized successful `PatternRetrievalResult`;
- the exact current `EvidenceItem` packet;
- the exact current `ExposureItem` records; and
- the exact current `MaterialEvent` packet.

Before any model call, validation checks:

- premise hash, source hashes, identity, and cutoff;
- state hash, standalone validation, symbol, exchange, direction, and premise
  linkage;
- evidence IDs, packet hash, publication cutoffs, and applicability;
- exposure IDs, types, categories, state linkage, and evidence references;
- event IDs, packet hash, references, cutoff, and future scheduled status;
- retrieval hash, validation, eligible recommendations, and state linkage;
- all six immutable source hashes; and
- decision-time cutoff isolation.

The provider receives serialized copies. Source objects are serialized before
and after execution, and mutation raises an implementation error.

## 5. Evidence Separation

The frozen provider packet keeps separate:

1. confirmed or otherwise status-labelled current `EvidenceItem` records;
2. current `ExposureItem` interpretations;
3. known `MaterialEvent` records, with future events allowed only as
   `scheduled`; and
4. generated `HypotheticalFutureEvent` conditions.

A hypothetical event:

- is explicitly marked hypothetical;
- uses conditional wording;
- records assumptions and uncertainty;
- has no source or evidence reference;
- has no fabricated exact date; and
- must be used by a typed scenario step.

Unknown fields such as a provider-invented source are rejected.

## 6. Pattern Grounding

Every non-insufficient scenario must cite at least one eligible retrieved
pattern.

Primary references may use only `retrieval.matches`. Counter references may
use only `retrieval.counter_patterns`. Each `ScenarioPatternGrounding`
preserves:

- exact pattern reference and version;
- lane;
- recommendation;
- match quality;
- reused matched feature tokens;
- all applicable differences; and
- all retrieval warnings.

A high rank without a matched causal feature is insufficient. Excluded,
unknown, wrong-lane, warning-omitting, or otherwise dangling pattern
references fail validation. When a counter-pattern exists, at least one
scenario must use it unless the set has a structured insufficient-evidence
state.

## 7. Causal Sequence

Every scenario contains contiguous, chronological `ScenarioStep` values.
Each step identifies:

- controlled step type;
- current evidence IDs;
- current exposure types;
- scheduled event IDs;
- hypothetical event IDs;
- controlled transmission mechanisms;
- expected market effect;
- timing relation;
- assumptions; and
- uncertainty.

Step references are checked against immutable input IDs and types. Step hashes
are application generated. A technical-invalidation scenario must contain an
`invalidation` step.

## 8. Target and Duration Linkage

Each narrative returns:

- conditional `target_linkage` text;
- conditional `duration_linkage` text;
- `linked_target_low`;
- `linked_target_high`;
- `linked_magnitude_bucket`; and
- `linked_duration_bucket`.

The structured values must exactly equal the frozen premise and current
state. The model may explain how a causal sequence could be consistent with
the move, but cannot change a value or claim exact event-to-price causation.

## 9. Scenario Diversity

A normal result contains three to six scenarios. Fewer scenarios require:

- an `insufficient_evidence` scenario; and
- an explicit `insufficient_evidence_reason`.

Where supported by current inputs, validation requires:

- a company-specific or event-driven scenario;
- a macro, liquidity, valuation, sector, or positioning scenario;
- a path that does not depend on company-specific bad news;
- a multi-factor scenario; and
- exactly one related technical-invalidation scenario.

Every primary scenario requires at least two independently traceable driver
families.

Diversity uses a deterministic feature signature:

- required exposure types;
- trigger types;
- transmission mechanisms;
- regime requirements;
- causal step types;
- supporting patterns; and
- counter-patterns.

Exact signature duplicates are rejected. Mechanism or pattern concentration
produces a warning. No embeddings or learned similarity are used.

## 10. Technical-Invalidation Scenario

The technical-invalidation scenario:

- has a direction opposing the frozen direction;
- uses `contradictory` premise compatibility;
- contains an `invalidation` causal step;
- is linked to a primary scenario through `invalidates` or
  `counter_scenario`; and
- preserves the exact application-generated invalidation token.

For a numeric invalidation level, the token is:

```text
price_invalidation_level:<canonical-number>
```

The scenario cannot replace or reinterpret this level.

## 11. Two-Pass Adversarial Review

Pass 1 produces the complete scenario set.

Pass 2 checks every scenario for:

- duplicate causal structure;
- unsupported future events;
- invented facts and dangling references;
- hindsight leakage;
- historical-analogue overreliance;
- ignored counter-patterns;
- missing contradicting evidence;
- weak transmission;
- target, magnitude, duration, and timing mismatch;
- certainty;
- one-catalyst explanations;
- missing technical invalidation; and
- confusion between scheduled and hypothetical events.

`revise` requires a complete replacement set. The replacement is validated
against the same immutable inputs. `keep`, `reject`, and
`insufficient_evidence` cannot contain a replacement. An invalid review
cannot pass through to acceptance.

## 12. Retry and Structured Failure

Each pass permits one to three attempts; the default is two.

Correctable output failures may retry:

- malformed JSON;
- missing fields;
- invalid enums;
- dangling references;
- target or duration mismatch;
- diversity or relationship failure;
- omitted contradictions, limitations, or invalidation;
- prohibited certainty or trade language;
- historical-pattern misuse; and
- hypothetical-event misuse.

Correction prompts include only deterministic validation feedback and forbid
new facts or references.

The service does not retry:

- invalid frozen inputs;
- unsupported or unavailable providers;
- packet-only mode;
- missing credentials or infrastructure failures;
- a genuine structured insufficient-evidence result; or
- lack of a suitable primary historical match.

Ordinary provider or output failures return a hashed
`CurrentScenarioGenerationResult`.

## 13. Schema and Prompt Versions

| Item | Version |
|---|---|
| Scenario step | `current-scenario-step-1.0.0` |
| Scenario narrative | `current-scenario-narrative-1.0.0` |
| Scenario relationship | `current-scenario-relationship-1.0.0` |
| Scenario set | `current-scenario-set-1.0.0` |
| Adversarial review | `current-scenario-adversarial-review-1.0.0` |
| Validation | `current-scenario-validation-1.0.0` |
| Generation result | `current-scenario-generation-result-1.0.0` |
| Synthesis prompt | `current-scenario-synthesis-prompt-1.0.0` |
| Adversarial prompt | `current-scenario-adversarial-prompt-1.0.0` |

The older report `ScenarioNarrative` and Market Scenario Report schema were
not changed.

## 14. Offline Fixtures

The fixture provider requires no network, credentials, database, market-data
feed, OpenAI, or Ollama.

Tests cover:

- valid bullish and bearish sets;
- company, macro, positioning, multi-factor, and technical-invalidation
  scenarios;
- three or more distinct causal structures;
- explicit hypothetical events;
- a hypothetical event incorrectly presented as confirmed;
- fabricated hypothetical dates and source fields;
- primary and counter-pattern grounding;
- unknown patterns and omitted counter-patterns;
- dangling evidence, event, relationship, and pattern references;
- target and duration mismatch;
- duplicate causal signatures;
- certainty, probability, Elliott-news prediction, and trade language;
- causal-step ordering;
- missing contradicting evidence;
- invalidation-token preservation;
- adversarial keep, revise, reject, and insufficient-evidence states;
- invalid adversarial replacements;
- malformed JSON and validation correction retries;
- genuine insufficiency without retry;
- unsupported, packet-only, and infrastructure provider states;
- immutable inputs;
- deterministic repeated runs;
- application-owned provenance and hashes; and
- serialization round trips.

## 15. Verification

Bundled runtime:

```text
C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
```

Compile and focused tests:

```powershell
python -m py_compile elliott_ai\current_scenarios.py elliott_ai\current_scenario_generator.py scripts\test_current_scenario_generator.py
python -m unittest scripts.test_current_scenario_generator -q
```

Result:

```text
Ran 51 tests in 8.633s
OK
```

Final full repository suite:

```powershell
python -m unittest discover -s scripts -p 'test_*.py' -q
```

Result:

```text
Ran 675 tests in 47.506s
OK
```

Exact outcome:

- passed: 675
- failures: 0
- errors: 0
- skipped: 0

## 16. Confirmed Restrictions

Phase 7 added no:

- live research or web access;
- database migration or persistence;
- CLI behavior;
- report integration;
- active-pipeline integration;
- Elliott changes;
- historical extraction or pattern-retrieval changes;
- machine learning;
- embeddings or vector search;
- probabilities;
- predictive confidence;
- analogue voting;
- trade execution or recommendations;
- portfolio construction; or
- automatic wave resolution.

## 17. Deferred Work

Separately approved future work remains:

- live news, filing, fundamental, macro, event, and web research adapters;
- current-evidence acquisition and freshness policy;
- production provider evaluation;
- persistence and audit-history design;
- CLI commands;
- Market Scenario report presentation;
- normal-pipeline integration after `resolve_degrees()`;
- operational monitoring and observability;
- prompt and schema migration policy for later versions; and
- production-quality evaluation against human-reviewed scenario outputs.
