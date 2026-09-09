# Phase 10 Completion Report

## Status

Phase 10, Company Knowledge Engine and Event-State Modeling, is complete.

The implementation is offline and deterministic. It accepts supplied
structured company data, creates immutable company snapshots, matches those
snapshots to a versioned event-archetype library, and supplies optional company
context to the existing scenario generator.

It does not fetch external data, predict events, assign probabilities, modify
Elliott Wave analysis, make trading decisions, or change the active pipeline.

## Files

Created:

- `elliott_ai/company_knowledge.py`
- `elliott_ai/company_knowledge_bridge.py`
- `scripts/test_company_knowledge.py`
- `PHASE10_COMPLETION_REPORT.md`

Modified:

- `elliott_ai/current_scenario_generator.py`
- `elliott_ai/market_scenario_evaluation.py`
- `docs/market_scenario_engine_spec.md`

No CLI, database, provider, reporting, Elliott analysis, pattern-library, or
active-orchestrator file was changed.

## Architecture

```text
CompanyKnowledgeInput
        |
        v
CompanyKnowledgeEngine
        |
        +-- CompanyProfile
        +-- CompanyFinancialState
        +-- CompanyOperationalState
        +-- CapitalStructureState
        +-- OwnershipPositioningState
        +-- ScheduledCompanyEvent
        +-- HistoricalCompanyEventProfile
        +-- CompanyDependency
        +-- CompanyStructuralRisk / Opportunity
        |
        v
CompanyKnowledgeSnapshot
        |
        v
CompanyEventCandidateBuilder
        |
        v
CompanyScenarioContext
        |
        +-- neutral EvidenceItem / MaterialEvent bridge
        +-- optional CurrentMarketScenarioGenerator input
        +-- standalone Phase 8 artifact bridge
        +-- Phase 9 company structural evaluation
```

The normal scenario path is unchanged unless a caller explicitly supplies a
`CompanyScenarioContext`.

## Enums

Primary controlled enums include:

- `CompanyStage`: `pre_revenue`, `early_revenue`, `growth`, `scaling`,
  `mature`, `restructuring`, `distressed`, `unknown`.
- `CompanyStateValue`: explicit strong/adequate/constrained/weak,
  improving/deteriorating, volatile/stable, high/moderate/low,
  positive/negative, profitable/unprofitable, present/absent,
  rising/falling, none, unknown, and unavailable states.
- `CompanyEventType`: earnings, investor, product/service/space launch,
  clinical, regulatory, contract, debt/refinancing, lockup, insider,
  litigation, management, production, delivery, index, option, and other
  scheduled event types.
- `CompanyEventStatus`: announced, scheduled, delayed, rescheduled, completed,
  cancelled, uncertain.
- `CompanyDependencyType`: customer, supplier, government, regulator, product,
  service, launch provider, manufacturer, commodity, currency, geography,
  interest rate, capital market, key person, technology, infrastructure,
  partner, contract, and single asset.
- `CompanyStructuralRiskType`: liquidity, financing, dilution, refinancing,
  concentration, execution, production, delay, regulatory, litigation,
  competition, margins, demand, valuation, management, technology, contract,
  backlog, geography, currency, commodity, rates, and dependency failure.
- `CompanyStructuralOpportunityType`: contract, product, launch, regulatory,
  customer, margin, demand, financing, partnership, acquisition, backlog,
  market, cost, technology, and capacity opportunities.
- `CompanyEventArchetypeType`: 29 event pathways covering earnings, guidance,
  launches, products, contracts, customers, financing, debt, regulation,
  litigation, management, operations, margins, backlog, positioning,
  valuation, and sector sympathy.
- `CompanyEventClassification`: known scheduled event, structural event risk,
  structural opportunity, hypothetical future event.
- `CandidateEligibilityStatus`: eligible, ineligible, duplicate, insufficient
  information.

Concentration, substitutability, replacement time, impact, recurrence, data
quality, and structural-condition status also use controlled enums.

## Schema Versions

The implementation introduces independent `1.0.0` versions for:

- `company-knowledge-input`
- `company-known-fact`
- `company-profile`
- `company-financial-state`
- `company-operational-state`
- `company-capital-structure`
- `company-ownership-positioning`
- `scheduled-company-event`
- `historical-company-event-profile`
- `company-dependency`
- `company-structural-risk`
- `company-structural-opportunity`
- `company-event-archetype`
- `company-event-archetype-library`
- `company-event-candidate`
- `company-knowledge-snapshot`
- `company-scenario-context`
- `company-market-input-bridge`
- `company-orchestration-bridge`
- `company-knowledge-replay`
- `company-knowledge-replay-result`
- `company-knowledge-build-result`
- `company-knowledge-validation`
- `company-scenario-usage-validation`
- `company-knowledge-evaluation`

Existing current-scenario schemas and stored JSON were not changed.

## Known Facts and Hypothetical Events

Known company facts are generated only from explicitly supplied, non-unknown
structured fields. Every fact keeps:

- its source input field;
- symbol;
- data-as-of value where supplied;
- applicable cutoff;
- source metadata; and
- deterministic content hash.

Known scheduled events require an explicit announcement timestamp and
`known_at_cutoff=true`. Announcement after the cutoff is a hard validation
failure. The scheduled date may be later than the cutoff because only the
known schedule is represented.

Structural risks and opportunities describe conditions. Historical event
profiles describe observed history. Neither class says that a future event
will occur.

Every event candidate remains conditional. It is labeled by classification,
keeps its supporting references, and states that it could become relevant only
if its explicit preconditions persist.

## Company Modeling

### Profile

The profile records identity, business and revenue models, products, services,
geography, customers, stage, operating history, cyclicality, capital
intensity, and regulatory intensity.

Missing stage or classification is `unknown`; it is never inferred from sector
or financial state.

### Financial State

Nineteen controlled financial fields cover revenue, growth, profitability,
margins, cash flow, cash, runway, debt, maturities, interest burden, liquidity,
working capital, capital expenditure, financing/dilution dependence, guidance,
estimate dispersion, and valuation.

Supplied quantitative values are retained unchanged. Contradictory qualitative
values become `unknown` with a deterministic warning. The engine does not
infer cash, runway, distress, maturity, dilution, or refinancing.

### Operational State

Seventeen common operational fields cover production, service, execution,
development, launch, manufacturing, supply chain, backlog, demand,
concentration, contracts, regulation, litigation, management, hiring,
geography, and infrastructure.

Typed extensions support space, biotechnology, and software without forcing
irrelevant fields on other companies.

### Capital Structure

Capital state records supplied share/dilution state, authorized capacity,
raise history, debt, convertibles, warrants, compensation dilution,
refinancing, covenants, maturities, capital access, and financing options.

It records constraints and optionality without concluding that a raise,
refinancing, or dilution will occur.

### Ownership and Positioning

Ownership state records supplied insider/institutional ownership,
concentration, shorts, borrow, options, lockups, insider transactions, index
membership, passive flows, and known positioning events. Unknown remains
explicit.

## Events, Dependencies, Risks, and Opportunities

Scheduled events retain announcement and schedule chronology, source identity,
date precision, recurrence, related entities, expected information, and
cutoff.

Dependencies retain concentration, substitutability, replacement time,
operational/financial impact, mitigants, provenance, and cutoff. They carry no
directional market implication.

Structural risks and opportunities require all fact, dependency, and scheduled
event references to resolve within the snapshot. They preserve preconditions,
invalidations, status, provenance, and hashes.

Historical event profiles preserve supplied observed counts, windows,
recurrence, outcome classes, preconditions, mechanisms, amplifiers, dampeners,
quality, and provenance. They do not imply recurrence.

## Archetype Library

The default library contains 29 deterministic archetypes. Every archetype
declares:

- stage and industry applicability;
- required and optional preconditions;
- contradictory conditions;
- event classification;
- controlled transmission mechanisms;
- amplifiers and dampeners;
- duration compatibility;
- exposure compatibility;
- frozen technical-direction compatibility; and
- falsification conditions.

Archetypes are immutable, content-hashed, versioned, and sorted by stable ID.
No LLM builds or selects an archetype.

## Candidate Generation

Candidate generation:

1. creates exact feature tokens only from supplied non-unknown state;
2. resolves every token to company source references;
3. checks stage, industry, preconditions, contradictions, duration, direction,
   exposures, and mechanisms;
4. preserves explicit exclusion reasons;
5. calculates a transparent relevance score;
6. detects duplicate causal signatures; and
7. orders eligible candidates first, then score descending, then stable ID.

The relevance score is:

| Component | Points |
|---|---:|
| Required-condition match | 0-45 |
| Optional-condition match | 0-20 |
| Compatible supplied exposure | 0 or 10 |
| Company/event context reference | 0 or 10 |
| Frozen direction compatibility | 0 or 7 |
| Frozen duration compatibility | 0 or 8 |
| Total | 0-100 |

The score is a matching score, not a probability. It cannot override a hard
incompatibility, contradiction, missing required condition, missing
transmission mechanism, or duplicate exclusion.

## Scenario-Generator Integration

`CurrentMarketScenarioGenerator.generate()` now accepts optional
`company_context` as its final argument.

When omitted:

- the old system prompt is unchanged;
- the old prompt packet is unchanged;
- no company field is required;
- input hashes are unchanged;
- current-scenario schemas are unchanged; and
- all prior generator tests retain their behavior.

When supplied:

- the context and context hash enter the frozen packet;
- a separately versioned system-prompt supplement is added;
- only eligible candidate IDs may be used;
- candidate event type and classification must be preserved;
- candidates must remain explicitly hypothetical;
- supporting company references and mechanisms are required;
- contradictory conditions and missing preconditions must be disclosed;
- amplifiers, dampeners, missing evidence, and invalidation are required; and
- certainty/probability language fails validation.

The bridge uses backward-compatible assumption tokens for company references.
It does not change the current-scenario output schema.

The neutral market-input bridge creates only neutral `EvidenceItem` values and
known `MaterialEvent` values. It deliberately returns an empty exposure tuple.

The standalone Phase 8 bridge reuses a completed immutable current state and
retrieval result. It is not installed into the active orchestrator.

## Phase 9 Evaluation

`CompanyKnowledgeEvaluation` is a separate Phase 9 structural result. It adds
metrics for:

- company-state completeness;
- financial, operational, and capital coverage;
- scheduled-event and dependency coverage;
- candidate grounding;
- hypothetical-label integrity;
- candidate duplication;
- archetype usage;
- company references;
- contradiction coverage; and
- invalidation coverage.

Hard gates cover:

- known/hypothetical confusion;
- invented company facts;
- invented scheduled events;
- invalid candidate references;
- company cutoff violations;
- snapshot mutation;
- unsupported archetypes; and
- a hypothetical event stated as fact.

The existing Phase 9 evaluation-suite contract is unchanged. No overall
probability or prediction score was added.

## Replay

The replay packet contains the full structured input, frozen technical
premise, current state, explicit exposures, exact archetype library, schema
manifest, and expected snapshot/candidate/context hashes.

Replay is fully offline and rebuilds each analytical artifact. Identical packet
inputs produced identical snapshot, candidate, context, and replay-result
hashes in the test suite.

## Tests

Focused Phase 10:

```text
python -m unittest scripts.test_company_knowledge
Ran 23 tests in 8.817s
OK
```

The tests cover structured company classes, sparse and contradictory data,
typed extensions, event cutoffs, dangling references, neutral bridging,
required candidate families, direction/stage/industry exclusions, duplicate
suppression, serialization, deterministic replay, scenario-generator context,
backward compatibility, standalone orchestrator bridging, and Phase 9 hard
gates.

Market Scenario Phase 1-10 subsystem:

```text
python -m unittest scripts.test_market_scenario scripts.test_exposure_engine scripts.test_historical_analyst scripts.test_historical_market scripts.test_historical_patterns scripts.test_historical_pattern_extractor scripts.test_current_state scripts.test_pattern_retrieval scripts.test_current_scenario_generator scripts.test_market_scenario_orchestrator scripts.test_market_scenario_evaluation scripts.test_company_knowledge
Ran 386 tests in 71.727s
OK
```

Replay, serialization, and standalone bridge:

```text
python -m unittest scripts.test_company_knowledge.CompanyReplayGeneratorEvaluationTests.test_replay_and_serialization_are_deterministic scripts.test_company_knowledge.CompanyReplayGeneratorEvaluationTests.test_context_serialization_round_trip scripts.test_company_knowledge.CompanyReplayGeneratorEvaluationTests.test_standalone_orchestrator_bridge_reuses_frozen_artifacts
Ran 3 tests in 1.935s
OK
```

Full repository:

```text
python -m unittest discover -s scripts -p "test_*.py"
Ran 763 tests in 102.320s
OK
```

Python compilation:

```text
python -m compileall -q elliott_ai scripts
Exit code: 0
```

An earlier subsystem command named two nonexistent test modules. The command
loaded 187 valid tests successfully but ended with two import errors for those
incorrect names. The corrected repository-specific subsystem command above is
the authoritative result.

## Compatibility

- Database migrations: **0**
- Existing scenario schema changes: **0**
- Existing CLI changes: **0**
- Existing provider changes: **0**
- Active orchestration changes: **0**
- Elliott Wave changes: **0**
- Historical pattern-library content changes: **0**

Additive compatibility considerations:

- `EvaluationCategory` and the Phase 9 hard-failure registry now contain
  company-knowledge values. Consumers that incorrectly assume an exhaustive
  old enum/set may need to accept additive values.
- `CurrentMarketScenarioGenerator.generate()` has one new optional final
  parameter. Existing positional and keyword calls remain valid.
- Company-context generator output keeps the old scenario schema, so its
  company linkage is represented by versioned assumption tokens and the
  generation input hash.
- Archetype definitions are deterministic policy, not learned truth. Future
  changes require a new library/policy version.

## Explicit Confirmations

- No external data, news, web, SEC, transcript, social, calendar, or
  market-data integration was added.
- No LLM builds or ranks company event candidates.
- No event is predicted.
- No probability, likelihood, expected return, or outcome score was added.
- No sentiment label was added.
- No Elliott Wave calculation or frozen technical premise was changed.
- No trade, portfolio, entry, exit, sizing, or recommendation logic was added.
- No active pipeline behavior was changed.
- No production data, company, event, reviewer, or candle fixture was
  fabricated.

## Deferred Work

Deferred beyond Phase 10:

- manual company-data entry and validation tooling;
- importers for saved company packets;
- production report rendering;
- active-pipeline and production-orchestrator integration;
- CLI commands;
- persistence or database schema;
- live research and external providers;
- monitoring, scheduling, alerts, and continuous refresh;
- deployment configuration;
- user interface;
- archetype governance and future version migration tooling; and
- any Phase 11 work.
