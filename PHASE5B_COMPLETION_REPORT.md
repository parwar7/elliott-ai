# Phase 5B Completion Report

## Scope

Phase 5B implements a deterministic reviewed historical-outcome evidence layer over analogues already selected by Phase 5A.4. It answers:

> What happened after these independently retrieved historical endpoint analogues?

It does not forecast the current endpoint, calculate an outcome likelihood, select a winning analogue, resolve an Elliott count, vote across outcomes, recommend a trade, or modify retrieval.

No later learning, recommendation, probabilistic, or automated wave-resolution phase was started.

## Authority and Boundary Check

`PHASE5A_TECHNICAL_DESIGN.md` remains absent from the workspace. This was already recorded in the Phase 5A.3 and Phase 5A.4 completion reports. The preserved Phase 3 through Phase 5A implementations and reports, current schemas and tests, and the explicit Phase 5B request were used as the governing contract.

The existing database already contained immutable reviewed outcomes and reviews with exact experience-case lineage. No migration was necessary or performed.

The implementation stays conservative where stored evidence is insufficient:

- no post-endpoint price series is fabricated;
- no fixed-bar horizon is invented;
- no directional aftermath is inferred from a wave label;
- no RSI, momentum, volume, or volatility aftermath is inferred unless explicitly stored;
- no descriptive aggregate is produced; and
- unavailable and censored evidence remains visible.

## Changed Files

| File | Change |
|---|---|
| `elliott_ai/experience_outcomes.py` | Adds Phase 5B specifications, taxonomy, event-based horizon, source validation, outcome evidence construction, retrieval freezing and attachment, hashes, explicit unavailable states, and text renderers. |
| `elliott_ai/knowledge.py` | Adds read-only outcome-source loading, one-case outcome audit, specification access, and retrieve-then-attach orchestration. |
| `elliott_ai/cli.py` | Adds `experience outcome-spec`, `experience outcomes`, and `experience evidence` without changing existing commands. |
| `scripts/test_experience_outcomes.py` | Adds 29 focused Phase 5B contract, linkage, state, leakage, CLI, determinism, and SQLite tests. |
| `ELLIOTT_AI_README.md` | Documents Phase 5B behavior, commands, horizon, unavailable metrics, and phase boundary. |
| `PHASE5B_COMPLETION_REPORT.md` | This report. |

No schema file, Phase 3 fingerprint, Phase 4 correction record, outcome, review, experience case, Pattern DNA row, or Phase 5A retrieval record was modified.

## Versioned Contracts

| Contract | Version |
|---|---|
| Outcome evidence schema | `experience-outcome-evidence-1.0.0` |
| Outcome evidence calculation | `experience-outcome-evidence-calc-1.0.0` |
| Outcome evidence specification | `experience-outcome-spec-1.0.0` |
| Reviewed outcome taxonomy | `reviewed-outcome-taxonomy-1.0.0` |
| Observation-horizon specification | `experience-observation-horizon-1.0.0` |
| Default horizon ID | `reviewed-resolution-window` |
| Default horizon version | `1.0.0` |

Authoritative hashes:

| Contract | SHA-256 |
|---|---|
| Phase 5B combined specification manifest | `2b8531fa39a7c6226e70b1bbc626dbf6418940dc8922f21961e5408e66e5da38` |
| Outcome taxonomy | `b2da045bcbc920eee25e77732f3ed591cac8377a3e6c0e7b08208a97110df4f8` |
| Observation horizon | `a6c663465561349dba92c2ffb5dab5af449cdbc58dc7a4c3fd0c18009e2323eb` |

Hashes use SHA-256 over ASCII canonical JSON with sorted keys, compact separators, finite values only, and the record's own `content_hash` removed from its calculation. No runtime timestamp is added to evidence output.

## Outcome Taxonomy

The versioned structural categories are:

| Category | Meaning |
|---|---|
| `reviewed_endpoint_completion` | Human review accepted the endpoint as a completed correction. |
| `reviewed_larger_corrective_role` | Human review reclassified the endpoint as a component of a larger corrective structure. |
| `reviewed_continuation` | Human review found that correction continued beyond the endpoint. |
| `reviewed_new_motive_role` | Human review assigned a new motive Wave 1 role. |
| `reviewed_corrective_role` | Human review assigned a corrective Wave A role. |
| `reviewed_local_terminal_role` | A local C or Y terminal role was accepted without asserting completion at the parent degree. |
| `structurally_ambiguous` | Human review did not resolve one structural interpretation. |

The mapping uses the existing Phase 4 hypothesis vocabulary. It does not reduce an outcome to bullish or bearish. Directional aftermath remains `structurally_unresolved` unless a future immutable source explicitly stores a reviewed directional observation.

## Observation Horizon

The only supported horizon is `reviewed-resolution-window` version `1.0.0`.

| Field | Rule |
|---|---|
| Unit | Event-based UTC time |
| Start | Immutable endpoint Pattern DNA cutoff |
| End | Accepted reviewed outcome resolution cutoff |
| Maximum available timestamp | Confirmation Pattern DNA cutoff |
| Complete | Maximum available timestamp is at or after the outcome resolution cutoff |
| Incomplete | Confirmation lineage stops before the outcome resolution cutoff |
| Censoring | Incomplete windows are explicitly right-censored |
| Fixed N-bar horizon | Unsupported because no immutable post-endpoint candle series is stored with an experience case |

Outcomes from another horizon cannot be treated as directly comparable without an explicit compatible horizon version.

## Accepted and Unavailable Evidence Rules

Accepted evidence requires all of the following:

1. the historical experience case is active and accepted by an explicitly human-confirmed experience review;
2. it is the exact case selected by the frozen Phase 5A.4 result;
3. the experience case, all three Pattern DNA rows, Phase 3 fingerprint, Phase 4 endpoint snapshot, correction case, outcome, outcome review, and their canonical hashes validate;
4. storage IDs, JSON source references, DNA references, and retrieval immutable-input hashes agree;
5. the linked outcome is the current reviewed revision and its linked review is the latest review for that revision;
6. the review action/status is accepted;
7. symbol, timeframe, Elliott degree, endpoint role, feed, provider, fingerprint, and snapshot provenance agree; and
8. the outcome resolution cutoff is strictly after the historical endpoint cutoff.

Explicit evidence states are:

- `accepted`
- `accepted_with_limitations`
- `unavailable_no_outcome`
- `unavailable_unreviewed_outcome`
- `unavailable_partially_reviewed_outcome`
- `unavailable_stale_outcome`
- `unavailable_experience_not_accepted`
- `rejected_outcome`
- `invalid_hash`
- `invalid_linkage`
- `source_load_error`

Reviewer disagreement is retained as a limitation and produces `accepted_with_limitations` when every acceptance and integrity rule otherwise passes. It does not silently discard evidence or pretend that reviewers agreed.

## Deterministic Outcome Fields

The current immutable sources support:

- outcome, experience, endpoint, fingerprint, snapshot, retrieval-position, and tier linkage;
- review action, status, reviewer, timestamp, rationale, and revisions;
- endpoint cutoff, outcome cutoff, maximum confirmation cutoff, completeness, and censoring;
- reviewed hypothesis, structural taxonomy, final interpretation, alternatives, and supporting reviewed structure;
- explicit Phase 4 confirmation events and their first observed timestamps;
- explicit stored channel-break behavior;
- elapsed time to reviewed resolution; and
- source and provenance hashes.

Formulas:

```text
elapsed_clock_seconds = UTC(outcome_resolution_cutoff) - UTC(endpoint_cutoff)
elapsed_clock_days = elapsed_clock_seconds / 86400
first_recorded_event = minimum observed_at among explicit confirmation events with status observed
```

The following remain explicit `unavailable` values because the schema does not contain a complete immutable post-endpoint candle series or terminal outcome price:

- maximum favorable excursion;
- maximum adverse excursion;
- signed or absolute movement;
- retracement and extension;
- drawdown;
- time to local price extreme;
- post-endpoint RSI behavior and divergence resolution;
- inferred momentum or volume expansion/contraction; and
- trendline retest behavior.

The system does not infer those measurements from the reviewed Elliott label.

## Retrieval and Outcome Isolation

The execution order is enforced in code:

1. `KnowledgeStore.build_experience_evidence()` runs the unchanged Phase 5A.2 filter and Phase 5A.3 comparison through `retrieve_experience_analogues()`.
2. Phase 5A.4 constructs the ordered result and content hash.
3. `attach_reviewed_outcome_evidence()` validates the Phase 5A.4 schema, calculation version, canonical hash, sequential presentation order, unique selected IDs, and retrieval tiers.
4. A separate frozen retrieval reference is content-hashed.
5. Only then is the outcome loader called, once and only once for each already-selected case ID in preserved order.
6. Every outcome source is validated against the immutable input hashes carried by that analogue.
7. The original Phase 5A.4 hash is recalculated after outcome loading and must remain unchanged.
8. Loaded case IDs must exactly equal the frozen selected IDs in the same order.

The result records an execution trace, selected IDs, loaded IDs, pre/post retrieval hashes, tier preservation, and order preservation.

Changing, adding, removing, or wording a reviewed outcome can change only the Phase 5B evidence record and combined Phase 5B hash. It cannot alter Phase 5A.2 eligibility, Phase 5A.3 comparison, Phase 5A.4 tier, order, limit, tie-break, selected IDs, or retrieval hash.

The existing Phase 5A.4 policy hash remains unchanged:

```text
926176ffcb013a748219d5978604aad4a9974038a13581632012b387c4a857a8
```

## Output Contract

Each combined evidence result contains:

- current endpoint identifier;
- frozen and original Phase 5A.4 retrieval references;
- unchanged retrieval hash and ordered selected IDs;
- evidence, taxonomy, and horizon versions and hashes;
- presentation options;
- one retrieval-evidence and historical-outcome-evidence pair per selected analogue;
- accepted and unavailable counts;
- outcome loader execution and isolation proof;
- provenance and source hashes;
- explicit issues, limitations, completeness, and censoring; and
- deterministic combined content hash.

Retrieval and outcome evidence are separate sibling sections. The text renderer labels the latter as historical reviewed evidence and states that it is contextual evidence, not a forecast.

## CLI

Inspect the Phase 5B contract:

```powershell
python -m elliott_ai experience outcome-spec --format json
python -m elliott_ai experience outcome-spec --format text
```

Audit one accepted experience outcome without changing retrieval:

```powershell
python -m elliott_ai experience outcomes EXPERIENCE_CASE_ID --format text --explain --show-provenance --show-hashes --include-outcome-details
```

Run Phase 5A.4, freeze it, and attach reviewed outcomes:

```powershell
python -m elliott_ai experience evidence CURRENT_CASE_ID --format json --output historical_outcome_evidence.json
python -m elliott_ai experience evidence CURRENT_CASE_ID --format text --explain --show-provenance --show-hashes --include-retrieval-details --include-outcome-details --output historical_outcome_evidence.md
```

Retrieval controls remain available:

```powershell
python -m elliott_ai experience evidence CURRENT_CASE_ID --candidate CASE_A --candidate CASE_B --comparison-level 3 --limit 5 --policy endpoint-evidence-lexicographic --policy-version 1.0.0 --horizon reviewed-resolution-window --horizon-version 1.0.0
```

The command also accepts the existing Phase 5A.2 filter switches. Existing CLI commands and options remain unchanged.

## Empty and Partial States

The empty accepted experience pool and no-retrieval state are valid:

- no exception;
- no fabricated outcome or analogue;
- no outcome-loader call;
- status `unavailable_no_retrieved_analogues`;
- zero analogue evidence records; and
- deterministic combined hash.

A mixed result preserves every retrieved position. An accepted outcome can appear beside a missing, unreviewed, partially reviewed, rejected, stale, invalid, or censored outcome. Unavailable evidence is never silently omitted.

The live accepted experience pool remains empty. No production experience or outcome was fabricated.

## Tests

The 29 new Phase 5B tests cover:

1. evidence, taxonomy, and horizon versions and stable hashes;
2. invalid horizon ID and version;
3. accepted outcome identity and linkage;
4. deterministic elapsed-time and event measurements;
5. explicit unavailable price metrics;
6. repeated-run determinism;
7. source and retrieval non-mutation;
8. separation of retrieval and outcome evidence;
9. no retrieved analogues and empty pool;
10. outcome missing;
11. outcome unreviewed;
12. outcome partially reviewed;
13. outcome rejected;
14. outcome stale;
15. experience not accepted;
16. source load failure;
17. outcome hash failure;
18. symbol, timeframe, degree, role, feed, and provider mismatch;
19. fingerprint and endpoint-snapshot mismatch;
20. storage-link mismatch;
21. outcome timestamp before endpoint;
22. incomplete and right-censored horizon;
23. missing market-data limitations;
24. reviewer disagreement;
25. mixed accepted/unavailable evidence;
26. all mandatory Phase 5A.2, 5A.3, and 5A.4 leakage invariants;
27. non-retrieved-case exclusion and retrieval-before-load ordering;
28. JSON/text/explanation/provenance/hash CLI and renderers; and
29. SQLite non-mutation, integrity, and zero foreign-key violations.

The mandatory leakage tests prove:

- changing a reviewed outcome does not alter Phase 5A.2 eligibility;
- changing a reviewed outcome does not alter Phase 5A.3 comparison;
- changing a reviewed outcome does not alter Phase 5A.4 tier, order, or hash;
- adding an outcome to a non-retrieved case does not select or load it;
- favorable wording cannot promote an analogue;
- unfavorable wording cannot demote an analogue; and
- an invalid retrieval fails before the outcome loader is called.

## Verification Results

Focused Phase 3 through Phase 5B command:

```powershell
C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest scripts.test_wave_fingerprints scripts.test_correction_state scripts.test_experience_engine scripts.test_experience_comparison scripts.test_experience_analogue scripts.test_experience_retrieval scripts.test_experience_outcomes
```

Result: 208 tests passed.

Complete suite command:

```powershell
C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s scripts -p "test_*.py"
```

Result on 21 July 2026:

| Metric | Count |
|---|---:|
| Tests run | 337 |
| Passed | 337 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |

The system Python environment lacks `pandas`, so the full suite used the configured bundled runtime. The Phase 5B-focused tests also pass under the system Python runtime.

## SQLite Integrity and Migration

Live database:

```text
C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in\.elliott_ai\elliott_ai.sqlite3
```

| Check | Result |
|---|---|
| `PRAGMA integrity_check` | `ok` |
| Foreign-key violations | 0 |
| Existing application table count | 29 |
| New Phase 5B tables | 0 |
| Phase 5B migrations | 0 |
| SQLite `user_version` | 0, unchanged |
| Experience cases | 0 |
| Experience reviews | 0 |
| Pattern DNA rows | 0 |
| Resolved outcomes | 0 |
| Outcome reviews | 0 |

## Compatibility and Limitations

1. No schema changed; old runs, JSON, tables, and CLI behavior remain readable.
2. The live pool is empty, so production evidence remains unavailable until reviewed Phase 5A.1 cases are accepted.
3. Outcome evidence is generated read-only and is not persisted as a new record.
4. No immutable post-endpoint candle series is linked to experience cases, so price-excursion and indicator-aftermath metrics remain unavailable.
5. Only the event-based reviewed-resolution horizon is supported. Fixed bar and calendar horizons need an approved immutable market-data source.
6. Reviewer disagreement is displayed but no consensus workflow is implemented.
7. No descriptive aggregation is implemented, avoiding confusion with current-case inference.
8. The requested Phase 5 technical-design Markdown remains absent.

## Deferred Work

Deferred to a separately approved later phase:

- immutable post-endpoint candle-window storage or references;
- fixed N-bar or calendar horizons;
- deterministic MFE, MAE, movement, retracement, extension, and drawdown formulas over approved price data;
- post-endpoint RSI, EWO, MACD, volume, volatility, channel, and trendline measurement;
- optional compatible-horizon descriptive summaries;
- reviewer-consensus and disagreement resolution;
- persisted combined evidence audit records;
- UI presentation beyond CLI text and JSON;
- production evaluation after accepted experience cases exist;
- machine learning, embeddings, vector search, adaptive or hidden weights;
- statistical calibration or current-case outcome inference;
- automatic Elliott resolution; and
- trading or position decisions.

Any future persistence requirement must receive a separate schema review and migration approval.

## Completion Confirmation

Phase 5B is complete. Phase 5A.2 filtering, Phase 5A.3 comparison, and Phase 5A.4 retrieval are unchanged. Reviewed outcomes are loaded only after retrieval validates and freezes, and only for already-selected case IDs.

No machine learning, embeddings, vector search, probabilities, prediction, outcome voting, automatic wave resolution, trade decision, outcome-aware ranking, adaptive weighting, hidden scoring, fabricated outcome, schema migration, or mutation of immutable Phase 3/4 records was added.
