# Phase 5A.4 Completion Report

## Scope

Phase 5A.4 implements a deterministic analogue-retrieval and presentation layer over:

1. historical experience cases accepted by the Phase 5A.1 human-review gate;
2. candidates that survived the Phase 5A.2 structural filter;
3. immutable endpoint comparisons produced by Phase 5A.3; and
4. cutoff-safe Phase 3 fingerprint and Phase 4 decision-snapshot references already carried by those comparisons.

It answers which eligible historical analogue records should be presented to a human reviewer first under an explicit, versioned policy.

It does not forecast future behavior, choose an Elliott count, calculate an outcome likelihood, select a trade, display reviewed outcomes, or guarantee that an analogue will repeat.

Phase 5B has not started.

## Authority and Boundary Check

The requested `PHASE5A_TECHNICAL_DESIGN.md` file is not present in the workspace or saved attachments. This was already documented during Phase 5A.3. The preserved Phase 5A design request, Phase 5A.1 through 5A.3 implementations and completion reports, and the explicit Phase 5A.4 request were used as the governing contract.

The missing file was handled conservatively:

- no reviewed outcome display was added;
- no outcome aggregation was added;
- no numerical grouped distance was invented;
- no blended similarity score was added;
- no diversity rule was invented;
- no query or match persistence was added;
- no database migration was performed.

The current request explicitly assigns deterministic retrieval presentation to Phase 5A.4, so that portion could be implemented without crossing into Phase 5B.

## Changed Files

| File | Change |
|---|---|
| `elliott_ai/experience_retrieval.py` | New versioned retrieval policy, tiering, lexicographic ordering, deterministic explanations, provenance/cutoff validation, result hashing, empty-pool handling, and text renderers. |
| `elliott_ai/knowledge.py` | Adds read-only retrieval-policy and analogue-retrieval APIs over the existing Phase 5A.2 and 5A.3 pipelines. |
| `elliott_ai/cli.py` | Adds `experience retrieve` and `experience retrieval-policy` without changing existing commands. |
| `scripts/test_experience_retrieval.py` | Adds 32 focused Phase 5A.4 tests. |
| `ELLIOTT_AI_README.md` | Documents the policy, tiers, ordering, CLI, empty-pool behavior, and outcome boundary. |
| `PHASE5A4_COMPLETION_REPORT.md` | This completion and verification report. |

No Phase 3 fingerprint, Phase 4 correction record, experience case, review, Pattern DNA row, accepted record, or SQLite schema object was modified.

## Versioned Contracts

| Contract | Version |
|---|---|
| Retrieval output schema | `experience-analogue-retrieval-1.0.0` |
| Retrieval calculation | `experience-analogue-retrieval-calc-1.0.0` |
| Retrieval policy schema | `experience-retrieval-policy-1.0.0` |
| Retrieval policy ID | `endpoint-evidence-lexicographic` |
| Retrieval policy version | `1.0.0` |
| Phase 5A.3 comparison specification | `experience-analogue-spec-1.0.0` |
| Phase 5A.2 filter schema | `experience-comparison-1.0.0` |

Authoritative retrieval-policy hash:

```text
926176ffcb013a748219d5978604aad4a9974038a13581632012b387c4a857a8
```

The policy is available in machine-readable or text form:

```powershell
python -m elliott_ai experience retrieval-policy --format json
python -m elliott_ai experience retrieval-policy --format text
```

Unsupported policy IDs, policy versions, comparison specifications, comparison levels, and dependency versions are rejected explicitly.

## Retrieval Policy

The policy permits Phase 5A.2 Levels 1 through 4. A dimension is eligible only at the levels declared by the existing Phase 5A.3 dimension specification. No broader dimension is silently substituted.

The mandatory structural dimensions are:

1. `endpoint_position`
2. `structural_role_class`
3. `parent_family`
4. `direction`
5. `elliott_degree`
6. `internal_structure_family`
7. `child_structure_status`
8. `completion_status_at_cutoff`
9. `start_pivot_alignment`
10. `end_pivot_alignment`

All 56 Phase 5A.3 dimensions remain in their existing specification order and remain attached as raw evidence to every returned analogue.

## Retrieval Tiers

### Tier 1: Exact Structural Analogue

Required conditions:

- Phase 5A.2 Level 1;
- exact role and family compatibility;
- every mandatory structural dimension comparable; and
- zero feed- or provenance-incomparable dimensions.

### Tier 2: Compatible Validated Analogue

Required conditions:

- Phase 5A.2 Level 1, 2, or 3;
- exact or compatible structural relationship;
- every mandatory structural dimension comparable; and
- at least one Tier 1 condition is not satisfied.

An otherwise exact Level 1 case with an optional raw feed incompatibility is therefore visible as Tier 2, not silently treated as Tier 1.

### Tier 3: Broader or Partially Available Analogue

This tier contains only cases that remain Phase 5A.2 eligible but are broader because they are:

- context-only;
- cross-market Level 4;
- missing a mandatory retrieval dimension;
- incomparable on a mandatory retrieval dimension; or
- otherwise outside Tier 1 and Tier 2 while still structurally eligible.

Tier 3 never repairs or bypasses a Phase 5A.2 exclusion.

## Deterministic Ordering

Candidates are ordered lexicographically. There is no weighted blend or overall numerical distance.

Primary keys:

1. retrieval tier, ascending;
2. Phase 5A.2 comparison level, ascending;
3. mandatory incomparable count, ascending;
4. mandatory missing count, ascending;
5. mandatory omitted/unsupported count, ascending;
6. mandatory comparable count, descending.

Group precedence:

1. endpoint identity and role;
2. structural geometry;
3. confirmation at cutoff;
4. momentum context;
5. volume context;
6. volatility context;
7. multi-timeframe context;
8. market context.

Within every group, the keys are:

1. incomparable count, ascending;
2. missing count, ascending;
3. omitted/unsupported count, ascending;
4. comparable count, descending;
5. outside-tolerance count, ascending;
6. within-tolerance count, descending;
7. categorical mismatch count, ascending;
8. exact categorical match count, descending.

The same eight keys are then applied to the aggregate dimension counts.

No raw numerical difference is mixed across units or feature families. Phase 5A.3 raw differences remain visible, but Phase 5A.4 does not blend them.

## Tie-Breaking

The complete tie sequence is:

1. primary retrieval keys;
2. each group summary in declared group precedence;
3. aggregate summary keys; and
4. stable `experience_case_id`, ascending, as the final tie-breaker.

Every returned case contains all ordering keys. Adjacent returned cases identify the first differing key, both values, and the preferred direction. Random selection is never used.

Cutoff proximity is metadata only and is not an ordering key. Case quality, reviewed outcome, later confirmation, profitability, and historical success are not ordering keys.

## Missing and Incomparable Data

Phase 5A.3 states are preserved exactly:

| State class | States | Retrieval behavior |
|---|---|---|
| Comparable | `comparable` | May contribute only to declared tolerance and categorical counts. |
| Missing | `missing_current`, `missing_historical`, `missing_both` | Increases missing counts and never becomes zero or a match. |
| Incomparable | `feed_incomparable`, `provenance_incomparable` | Increases incomparability counts and never becomes a distance. |
| Omitted | `structurally_inapplicable`, `unsupported_by_spec` | Increases omitted counts and remains explicitly identified. |

Missing or incomparable mandatory evidence moves a case to Tier 3. Optional feed/provenance incomparability prevents Tier 1 but does not structurally invalidate a Phase 5A.2-eligible case.

## Output Contract

Every retrieval result contains:

- current endpoint identifier;
- policy ID, version, and hash;
- comparison-specification version and hash;
- Phase 5A.2 filter and matrix versions;
- requested result limit and maximum comparison level;
- eligible, excluded, unavailable, presented, and limit-omitted counts;
- ordered experience-case references;
- retrieval tier and tier explanation;
- all deterministic ordering keys;
- adjacent-order explanation;
- raw Phase 5A.3 dimension evidence;
- missing, incomparable, and omitted records;
- Phase 5A.2 filter-decision reference;
- Phase 5A.3 comparison ID and hash;
- endpoint DNA, Phase 3 fingerprint, and Phase 4 snapshot hashes;
- provenance and cutoff verification;
- warnings; and
- canonical result content hash.

Hashes use SHA-256 over ASCII canonical JSON with sorted keys, compact separators, finite numbers only, and the `content_hash` field excluded from its own calculation. No retrieval timestamp is added, so repeated identical input is byte-for-byte deterministic.

## CLI

JSON retrieval:

```powershell
python -m elliott_ai experience retrieve CURRENT_CASE_ID --format json --output analogue_retrieval.json
```

Human-readable retrieval with complete audit detail:

```powershell
python -m elliott_ai experience retrieve CURRENT_CASE_ID --format text --explain --show-ordering-keys --show-provenance --show-hashes --include-comparison-details --output analogue_retrieval.md
```

Candidate, level, policy, and limit controls:

```powershell
python -m elliott_ai experience retrieve CURRENT_CASE_ID --candidate CASE_A --candidate CASE_B --comparison-level 3 --limit 5 --policy endpoint-evidence-lexicographic --policy-version 1.0.0
```

`--level` is an alias for `--comparison-level`. The command also accepts:

```text
--spec-version VERSION
--same-degree-only
--no-adjacent-degree
--allow-far-degree
--allow-cross-market
--allow-context-family
```

Existing CLI commands and options remain unchanged.

## Empty Experience Pool

The empty accepted pool is a supported production state.

Behavior:

- no exception;
- no fabricated candidate;
- no placeholder analogue;
- status `unavailable_empty_accepted_experience_pool`;
- explicit explanation that no accepted historical case is available after excluding the current case; and
- deterministic content hash for identical current endpoint, comparison input, and policy.

The canonical test fixture empty-result hash is:

```text
5ad02174c5401a782c1b57e00f5c2142560473de8f57a12c16406b71e5802616
```

An empty-result hash is intentionally current-endpoint-specific, so another current endpoint produces a different but repeatable hash.

## Reviewed Outcome Isolation

`KnowledgeStore.retrieve_experience_analogues()` calls the existing endpoint-only comparison path. `KnowledgeStore._experience_comparison_record()` loads case status, endpoint DNA, and immutable endpoint-source integrity only. It does not load confirmation DNA or resolved-outcome DNA.

The Phase 5A.4 module imports no outcome-resolution API and reads no outcome field.

The dedicated outcome-invariance test changes reviewed outcomes and confirmation/outcome payloads on both current and historical source records. The following remain byte-for-byte unchanged:

- Phase 5A.2 eligibility and exclusion output;
- Phase 5A.3 comparison output;
- retrieval order;
- retrieval tier;
- ordering keys; and
- retrieval content hash.

Reviewed outcomes were not used in filtering, comparison, tiering, ordering, selection, tie-breaking, or hashing.

## Tests

The 32 new Phase 5A.4 tests cover:

1. policy fields, versions, ordering declarations, and stable hash;
2. invalid policy ID;
3. invalid policy version;
4. invalid comparison level;
5. invalid result limits;
6. retrieval output versions, hash, and source links;
7. tampered Phase 5A.3 input rejection;
8. empty accepted experience pool;
9. one eligible candidate;
10. multiple eligible candidates;
11. all four Phase 5A.2 comparison levels;
12. all three retrieval tiers;
13. feed incomparability;
14. provenance incomparability;
15. mandatory-dimension absence;
16. structurally inapplicable dimensions;
17. comparison-level omission and unavailable counts;
18. lexicographic level precedence;
19. tolerance and categorical summary ordering;
20. exact categorical match ordering;
21. stable case-ID final tie-break;
22. missing historical values;
23. missing current values;
24. missing-both values;
25. result limits without eligibility mutation;
26. repeated-run determinism and source non-mutation;
27. structural exclusion preservation;
28. cutoff leakage prevention;
29. complete reviewed-outcome invariance;
30. outcome/confirmation payload exclusion;
31. JSON/text/explanation/provenance/hash CLI and renderer output; and
32. SQLite non-mutation, integrity, and zero foreign-key violations.

No grouped numerical distance test was required because this implementation deliberately does not create a grouped distance.

## Verification Results

Focused Phase 3 through Phase 5A.4 command:

```powershell
python -m unittest scripts.test_wave_fingerprints scripts.test_correction_state scripts.test_experience_engine scripts.test_experience_comparison scripts.test_experience_analogue scripts.test_experience_retrieval
```

Result: 179 tests passed.

Complete suite command, using the bundled workspace Python runtime that includes pandas:

```powershell
C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s scripts -p "test_*.py"
```

Result on 21 July 2026:

| Metric | Count |
|---|---:|
| Tests run | 308 |
| Passed | 308 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |

The system Python environment does not currently include pandas, so the complete suite was run with the configured bundled workspace runtime. The code and Phase 5A.4 focused tests also pass under the system Python runtime.

## SQLite Integrity

Live database:

```text
C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in\.elliott_ai\elliott_ai.sqlite3
```

| Check | Result |
|---|---|
| `PRAGMA integrity_check` | `ok` |
| Foreign-key violations | 0 |
| Existing application table count | 29 |
| New Phase 5A.4 tables | 0 |
| Experience cases | 0 |
| Pattern DNA rows | 0 |
| Experience reviews | 0 |
| Accepted historical experience pool | 0 |

## Compatibility Risks

1. The named approved technical-design Markdown file remains absent. The explicit Phase 5A.4 request is therefore the retrieval authority.
2. The live accepted pool is empty, so production retrieval remains unavailable until Phase 5A.1 cases are deliberately human-reviewed and accepted.
3. The policy requires the complete current Phase 5A.3 56-dimension record and supported hashes. Future schema versions need an explicit adapter or new policy version.
4. Tier 3 deliberately preserves broad and partial cases. Human output must retain the tier reason, missing fields, incomparability, and warnings.
5. No raw numerical distances are blended. Two otherwise tied records can therefore reach the stable case-ID tie-break after their declared count summaries match.
6. JSON retrieval includes raw comparison evidence and can be large.
7. Retrieval is read-only and not persisted. Reproduction depends on retaining the source comparison input and policy version/hash.
8. Diversity controls are absent by design, so one symbol or episode family can occupy several returned positions if separate cases remain structurally eligible.
9. Result hashes include the current endpoint and source comparison hash; they are deterministic but not universal across queries.

## Deferred to Phase 5B or Later

- reviewed outcome display after retrieval;
- historical observed-outcome summaries or frequencies;
- outcome aggregation across returned analogues;
- canonical-memory promotion;
- reviewer relevance feedback;
- production retrieval evaluation once accepted cases exist;
- any approved deterministic diversity matrix;
- persisted query/match audit tables, if separately approved;
- unified contextual-experience evidence inside model analysis;
- embeddings or vector databases;
- learned, adaptive, or outcome-derived weights;
- statistical calibration;
- machine learning;
- automatic Elliott resolution; and
- automated trade or position decisions.

## Completion Confirmation

Phase 5A.4 is complete. Reviewed outcomes were not used. No machine learning, embeddings, vector database, prediction, calibrated probability, automatic wave resolution, trade decision, adaptive weighting, hidden weighting, diversity rule, grouped numerical distance, or database migration was added.

The empty accepted pool remains valid and no production experience case was fabricated.
