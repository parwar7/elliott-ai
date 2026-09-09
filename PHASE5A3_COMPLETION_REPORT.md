# Phase 5A.3 Completion Report

## Scope

Phase 5A.3 implements a deterministic, explainable analogue-comparison layer over endpoint DNA that has already survived the Phase 5A.2 structural filter. It answers how a current endpoint differs from each eligible reviewed historical endpoint.

It does not select a best analogue, rank cases by relevance, read reviewed outcomes as features, calculate probabilities, predict a wave, resolve an Elliott count, or produce a trade decision.

Phase 5A.4 and Phase 5B have not started.

## Pre-Implementation Authority Check

The requested `PHASE5A_TECHNICAL_DESIGN.md` file was not present in the workspace, nearby Codex folders, or saved attachments. The preserved approved Phase 5A design request, Phase 5A.1 and 5A.2 implementations and completion reports, and the explicit Phase 5A.3 request were used as the authoritative contract.

The missing file created one conservative boundary decision: Phase 5A.3 emits per-dimension differences in stable identity order but no cross-case ranking, best-analogue selection, grouped score, or overall distance. Those decisions remain deferred rather than inferred.

Post-endpoint Phase 4 confirmations also remain excluded. The only confirmation-context dimensions are values already present at the endpoint cutoff: child-structure status, completion status, channel terminal state, and start/end pivot alignment.

## Changed Files

| File | Change |
|---|---|
| `elliott_ai/experience_analogue.py` | New versioned 56-dimension specification, seven comparison methods, explicit missing/incomparability states, level gates, provenance, hashes, endpoint-pair/set comparison, and human renderer. |
| `elliott_ai/experience_comparison.py` | Adds an optional hard exclusion when store-verified immutable fingerprint or endpoint-snapshot integrity fails. |
| `elliott_ai/knowledge.py` | Verifies immutable Phase 3/4 source references and adds read-only specification and comparison APIs. |
| `elliott_ai/cli.py` | Adds `experience compare` and `experience comparison-spec` with JSON/text, level, candidate, explanation, provenance, hash, and filter controls. |
| `scripts/test_experience_analogue.py` | Adds 32 focused Phase 5A.3 tests. |
| `ELLIOTT_AI_README.md` | Documents Phase 5A.3 formulas, states, commands, feed policy, and phase boundary. |
| `PHASE5A3_COMPLETION_REPORT.md` | This completion and verification report. |

No SQLite schema, table, column, Phase 3 fingerprint, Phase 4 snapshot/outcome, Pattern DNA row, experience review, or approved historical record was modified.

## Versioned Contracts

| Contract | Version |
|---|---|
| Comparison output schema | `experience-analogue-comparison-1.0.0` |
| Comparison calculation | `experience-analogue-comparison-calc-1.0.0` |
| Comparison specification | `experience-analogue-spec-1.0.0` |
| Dimension specification | `experience-analogue-dimensions-1.0.0` |
| Phase 5A.2 filter dependency | `experience-comparison-1.0.0` |

Authoritative specification hash:

```text
ee69186a98a127d21716e851889cfc834366e4bc31d9a7333b1a751c158f73e8
```

The full machine-readable specification is available through:

```powershell
python -m elliott_ai experience comparison-spec
```

An unsupported specification version is rejected explicitly; it is never silently interpreted as the current version.

## Comparison Specification

The specification contains 56 dimensions: 35 difference-producing and 21 descriptive-only.

| Group | Count | Dimensions |
|---|---:|---|
| Endpoint identity and role | 10 | position, structural role, parent family, direction, degree, timeframe, symbol, provider, feed, market type |
| Structural geometry | 15 | percentage move, log return, normalized range, efficiency, candle/clock duration, retracement, extension, overlap, pivots, child count/family, acceleration, channel fit/deviation |
| Momentum context | 10 | EWO peak/timing/zero-crosses, MACD peak/timing/histogram state, RSI end/slope/divergence/proportion above 50 |
| Volume context | 6 | baseline ratio, terminal/median ratio, peak location, effort/result, proportion above baseline, participation regime |
| Volatility context | 5 | state, ATR percent, Bollinger/Keltner pierces, terminal Bollinger position |
| Multi-timeframe context | 2 | parent wave identifier, broader regime |
| Market context | 3 | session, venue, quote currency |
| Confirmation at cutoff | 5 | child status, completion, channel terminal state, start pivot alignment, end pivot alignment |

Every dimension definition records:

- dimension ID and human-readable name;
- endpoint DNA source path or deterministic derivation;
- data type;
- explicit comparison method;
- normalization rule;
- missing and incomparability behavior;
- inclusive tolerance rule where applicable;
- output format;
- descriptive-only or difference-producing status;
- allowed progressive levels;
- feed/timeframe/volume-scope requirements;
- specification version.

## Progressive Levels

The Phase 5A.2 level assigned to each historical candidate is preserved. The caller can set a maximum level but cannot relabel a candidate or broaden a dimension silently.

| Level | Permitted dimensions | Policy |
|---:|---:|---|
| 1 | 56 | Exact role/family and same degree; all specification dimensions may be attempted subject to provenance. |
| 2 | 54 | Exact role/family and adjacent degree; raw EWO and raw MACD peak magnitudes are omitted. |
| 3 | 56 | Compatible or explicitly enabled context/far-degree structure; provenance requirements still apply. |
| 4 | 38 | Cross-market normalized/context dimensions only; raw identity- and feed-bound fields are omitted. |

Level permission does not guarantee data availability. A permitted field can still be missing, feed-incomparable, provenance-incomparable, or unsupported.

## Comparison Formulas

Let `c` be the normalized current value and `h` the normalized historical value.

### Signed and Absolute Difference

```text
signed_difference = c - h
absolute_difference = abs(c - h)
```

### Ratio Difference

```text
ratio = c / h
signed_ratio_delta = ratio - 1
absolute_ratio_delta = abs(ratio - 1)
```

When `h == 0`, ratio comparison returns `unsupported_by_spec`; it does not invent zero, infinity, or a favorable match.

### Tolerance Band

```text
within_tolerance = observed_metric <= versioned_threshold
```

The boundary is inclusive. Raw values, the observed metric, threshold, and result remain visible.

### Categorical Exact Match

```text
exact_match = normalize(c) == normalize(h)
```

### Categorical Compatibility

Role and family values use the existing versioned Phase 5A.2 compatibility matrices. The output preserves compatibility class, rule ID, reason, matrix location, and matrix version.

### Ordered-State Distance

```text
signed_order_distance = index(c) - index(h)
absolute_order_distance = abs(index(c) - index(h))
```

Orders are explicit in the dimension definition, for example `contracting -> stable -> expanding`.

### Descriptive Pair

Both values and their same/different relationship are emitted, but no numerical distance is produced.

### Deterministic Derivation

Volume participation uses the stored normalized average-volume ratio:

```text
ratio < 0.8       -> contracting
0.8 <= ratio <= 1.2 -> balanced
ratio > 1.2       -> expanding
```

There are no grouped weights, hidden weights, adaptive weights, learned weights, grouped scores, or overall distance.

## Missing and Incomparability Rules

The exact per-dimension states are:

| State | Meaning |
|---|---|
| `comparable` | Both values validated and the explicit method ran. |
| `missing_current` | Current endpoint value is null or absent. |
| `missing_historical` | Historical endpoint value is null or absent. |
| `missing_both` | Both endpoint values are null or absent. |
| `structurally_inapplicable` | The dimension is not permitted at the candidate's Phase 5A.2 level. |
| `feed_incomparable` | The raw dimension requires an identical provider/feed and they differ. |
| `provenance_incomparable` | Timeframe, volume scope, or mandatory provenance is absent or incompatible. |
| `unsupported_by_spec` | The method, category, ratio denominator, or numeric value is invalid for this specification. |

`NaN`, positive/negative infinity, booleans, and numeric strings are rejected for numeric dimensions. Missing and incomparable data never become zero and never improve a comparison.

Raw EWO/MACD requires the same provider, feed, and timeframe. RSI requires the same feed and timeframe. Candle counts and band-pierce counts require the same timeframe. Normalized volume ratios can cross feeds only when the volume scope agrees. Phase 5A.2 incomparable-group metadata is preserved.

## Output Contract

Every historical comparison contains:

- current endpoint and historical experience identifiers;
- exact Phase 5A.2 filter-result reference and hash;
- specification version and hash;
- all 56 dimension records, including omitted records;
- exact comparable, missing, incomparable, and omitted dimension lists;
- per-dimension current/historical raw and normalized values;
- raw difference and tolerance result;
- comparison-level result and reason;
- source and feature-group provenance;
- endpoint DNA, fingerprint, and decision-snapshot hashes;
- cutoff validation;
- inherited non-scored warnings;
- deterministic comparison ID and content hash.

Hashes use SHA-256 over ASCII canonical JSON with sorted keys, compact separators, finite numbers only, and the `content_hash` field excluded from its own calculation. There is no timestamp in the comparison payload, so repeated identical input is byte-for-byte deterministic.

## Immutable Source Verification

The store comparison path validates:

1. Pattern DNA row hash, JSON hash, and canonical endpoint-DNA hash agree.
2. Stored Phase 3 fingerprint row hash, fingerprint payload hash, endpoint source hash, and Pattern DNA row source hash agree.
3. Stored Phase 4 decision-snapshot ID and canonical snapshot hash agree with endpoint DNA.
4. Endpoint and feature-group cutoffs pass the Phase 5A.2 leakage checks.

An explicit source-reference integrity failure is a Phase 5A.2 hard exclusion: `HARD-INVALID-IMMUTABLE-REFERENCE`.

## Endpoint and Outcome Isolation

`KnowledgeStore._experience_comparison_record()` loads only case status, endpoint DNA, and immutable endpoint-source integrity. It does not load confirmation DNA or resolved-outcome DNA.

The comparator reads only `endpoint_dna`. Tests inject different `confirmation_dna`, `resolved_outcome_dna`, and reviewed-outcome payloads into both current and historical records; output remains byte-for-byte identical.

Human review state is used only by Phase 5A.2 to decide whether a historical case is eligible. The reviewed outcome, rejected alternatives, future confirmation events, and eventual market behavior are never comparison dimensions or ordering inputs.

## CLI

Inspect the specification:

```powershell
python -m elliott_ai experience comparison-spec
```

JSON output:

```powershell
python -m elliott_ai experience compare CURRENT_CASE_ID --format json --output analogue_comparison.json
```

Human-readable output with full explanation:

```powershell
python -m elliott_ai experience compare CURRENT_CASE_ID --format text --explain --show-provenance --show-hashes --output analogue_comparison.md
```

Candidate and level controls:

```powershell
python -m elliott_ai experience compare CURRENT_CASE_ID --candidate CASE_A --candidate CASE_B --level 2
```

The command also accepts:

```text
--spec-version VERSION
--same-degree-only
--no-adjacent-degree
--allow-far-degree
--allow-cross-market
--allow-context-family
```

`--level` means the maximum progressive level to include. A structurally eligible candidate above that level is reported as omitted by the request, not silently discarded or relabeled.

## Tests

The 32 new Phase 5A.3 tests cover:

1. Explicit specification fields, versions, uniqueness, and stable hash.
2. Unsupported specification versions.
3. All seven comparison methods.
4. Signed and absolute numeric difference.
5. Ratio difference and zero-reference handling.
6. Categorical exact and descriptive comparison.
7. Matrix-backed categorical compatibility.
8. Ordered-state distance.
9. Inclusive tolerance boundaries.
10. Invalid and non-finite numeric values.
11. All eight required dimension states.
12. Level 1 raw momentum behavior.
13. Level 2 raw-momentum omission and normalized geometry.
14. Level 3 compatible roles.
15. Level 4 normalized/context-only behavior.
16. Feed incompatibility.
17. Volume-scope incompatibility.
18. Phase 5A.2 hard-exclusion preservation.
19. Cutoff leakage prevention.
20. Confirmation/outcome leakage prevention.
21. Deterministic repeatability.
22. Comparison and set hash stability.
23. No mutation of current or historical source records.
24. Rejection of noneligible direct pair input.
25. Empty candidate pool.
26. One-candidate pool.
27. Multiple-candidate stable identity order.
28. Candidate and maximum-level selection.
29. Immutable-reference failure exclusion.
30. Real stored Phase 3/4 immutable-reference validation.
31. CLI JSON and human-readable explanation output.
32. Empty-pool renderer and exposed versions.

## Verification Results

Focused Phase 3 through Phase 5A.3 command:

```powershell
python -m unittest scripts.test_wave_fingerprints scripts.test_correction_state scripts.test_experience_engine scripts.test_experience_comparison scripts.test_experience_analogue
```

Result: 147 tests passed.

Complete suite command:

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```

Result on 21 July 2026:

| Metric | Count |
|---|---:|
| Tests run | 276 |
| Passed | 276 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |

## SQLite Verification

| Check | Result |
|---|---|
| `PRAGMA integrity_check` | `ok` |
| Foreign-key violations | 0 |
| Existing table count | 29 |
| New Phase 5A.3 tables | 0 |
| Experience cases in live database | 0 |
| Pattern DNA rows in live database | 0 |
| Experience reviews in live database | 0 |
| Accepted experience pool | 0 |

Cold start therefore returns `unavailable_no_eligible_candidates`; no analogue is fabricated.

## Compatibility Risks

1. The live accepted pool is empty, so real analogue output remains unavailable until Phase 5A.1 cases are deliberately human-accepted.
2. Many existing fingerprints do not have sibling-based Fibonacci ratios or optional RSI. Those dimensions correctly remain missing rather than lowering a distance.
3. Generic Corrective A endpoint DNA still requires a real internal family before Phase 5A.2 can admit it.
4. Future endpoint or comparison specification versions are rejected until an explicit adapter is implemented.
5. Incomplete optional market context can leave regime, parent-wave, venue, or quote-currency dimensions missing.
6. Cross-market Level 4 is deliberately narrower and may contain many missing normalized dimensions.
7. Human-readable `--explain` output is intentionally large because it shows every permitted, missing, incomparable, and omitted dimension.
8. Pure function callers can supply endpoint records directly; only the `KnowledgeStore` path can verify that referenced fingerprint and snapshot payloads physically exist in SQLite.
9. The named approved technical-design Markdown file remains absent; ranking was therefore conservatively deferred instead of reconstructed.

## Deferred Phase 5A.4 Work

- reviewed outcome display after comparison;
- historical outcome aggregation and observed frequencies;
- cross-case reporting and presentation policy;
- retrieval evaluation and known-analogue benchmarks;
- any explicitly approved relevance ordering or ranking policy;
- canonical-memory promotion and reviewer relevance feedback;
- persisted query/match audit tables if separately approved.

## Deferred Phase 5B Work

- machine learning;
- embeddings or vector databases;
- learned or adaptive weights;
- statistical calibration;
- probabilities and predictive models;
- automatic Elliott resolution;
- automated trading or position management.

## Phase Boundary

Phase 5A.3 is complete. Reviewed outcomes were not used as comparison inputs. No machine learning, probability, prediction, automatic wave resolution, trade decision, grouped score, best-analogue selection, or cross-case ranking was added.
