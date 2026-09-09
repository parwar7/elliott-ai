# Phase 5A.2 Completion Report

## Scope

Phase 5A.2 implements deterministic structural comparison classes and endpoint-only filtering for reviewed Elliott experience cases. It answers which historical cases are structurally eligible for later comparison. It does not calculate similarity, rank cases, aggregate outcomes, train a model, create probabilities, or select an Elliott count.

Phase 5A.3, Phase 5A.4, and Phase 5B have not started.

## Changed Files

| File | Change |
|---|---|
| `elliott_ai/experience_comparison.py` | New versioned role classes, exhaustive role/family matrices, degree policy, endpoint-DNA validation, deterministic filtering, soft metadata, and explainable exclusions. |
| `elliott_ai/knowledge.py` | Adds read-only store adapters that load endpoint DNA and run the filter without persisting a query or match. |
| `elliott_ai/cli.py` | Adds `experience filter` and `experience matrix` commands and configuration flags. |
| `scripts/test_experience_comparison.py` | Adds 26 focused Phase 5A.2 tests. |
| `ELLIOTT_AI_README.md` | Documents Phase 5A.2 behavior, defaults, commands, and feed handling. |
| `PHASE5A2_COMPLETION_REPORT.md` | This implementation and verification report. |

No database table, column, accepted record, approved historical record, Phase 3 fingerprint, Phase 4 outcome, or existing Pattern DNA record was changed.

## Versioned Contracts

| Contract | Version |
|---|---|
| Comparison output schema | `experience-comparison-1.0.0` |
| Filter calculation | `experience-comparison-filter-1.0.0` |
| Structural role classes | `structural-role-class-1.0.0` |
| Role compatibility matrix | `structural-compatibility-matrix-1.0.0` |
| Family compatibility matrix | `structural-family-matrix-1.0.0` |

## Explicit Role Classes

The implementation defines 31 roles. Corrective A has no letter-only or unknown fallback: its actual internal family must be available.

| Group | Explicit classes |
|---|---|
| Impulse | `impulse.wave_1` through `impulse.wave_5` |
| Zigzag | `zigzag.a`, `zigzag.b`, `zigzag.c` |
| Flat | `flat.a`, `flat.b`, `flat.c` |
| Triangle | `triangle.a` through `triangle.e` |
| Double three | `double_three.w`, `double_three.x`, `double_three.y` |
| Triple three | `triple_three.w`, `triple_three.x`, `triple_three.y`, `triple_three.x2`, `triple_three.z` |
| Corrective A | `corrective_a.impulse`, `.diagonal`, `.zigzag`, `.flat`, `.triangle`, `.double_three`, `.triple_three` |

Role classification uses the parent pattern family, structural position, and actual internal family. A declared endpoint role is supported for explicit versioned data. Unsupported or unresolved combinations are excluded rather than guessed from a wave letter.

## Compatibility Matrix

The generated role matrix is exhaustive: 31 x 31 = 961 explicitly classified cells.

| Role compatibility | Cells |
|---|---:|
| `exact_match` | 31 |
| `compatible` | 22 |
| `context_only` | 60 |
| `incompatible` | 848 |

The family matrix is also exhaustive: 9 x 9 = 81 cells.

| Family compatibility | Cells |
|---|---:|
| `exact_match` | 8 |
| `compatible` | 4 |
| `context_only` | 22 |
| `incompatible` | 47 |

The complete cell-by-cell matrices, rule IDs, reasons, and versions are returned by:

```powershell
python -m elliott_ai experience matrix
```

### Role Rules

| Pair or group | Class | Rule |
|---|---|---|
| Same explicit role | `exact_match` | `ROLE-EXACT` |
| Zigzag C and Flat C | `compatible` | `ROLE-TERMINAL-C` |
| Double-three and triple-three at the same W, X, or Y position | `compatible` | `ROLE-COMBINATION-SAME-POSITION` |
| Zigzag A and motive/diagonal Corrective A | `compatible` | `ROLE-ZIGZAG-A-INTERNAL-MOTIVE` |
| Flat A and corrective-family Corrective A | `compatible` | `ROLE-FLAT-A-INTERNAL-CORRECTIVE` |
| Corrective A impulse and Corrective A diagonal | `compatible` | `ROLE-CORRECTIVE-A-MOTIVE-VARIANTS` |
| Wave 1 and motive/diagonal Corrective A | `context_only` | `ROLE-WAVE1-VS-CORRECTIVE-A` |
| Zigzag and Flat at the same A or B position | `context_only` | `ROLE-CORRECTION-SAME-LETTER-CONTEXT` |
| Different motive positions 1, 3, and 5 | `context_only` | `ROLE-IMPULSE-SIBLING-CONTEXT` |
| Different corrective positions 2 and 4 | `context_only` | `ROLE-IMPULSE-SIBLING-CONTEXT` |
| Different Corrective A internal families | `context_only` unless a more specific rule applies | `ROLE-CORRECTIVE-A-INTERNAL-CONTEXT` |
| Triangle leg and any non-identical role | `incompatible` | `ROLE-TRIANGLE-ISOLATION` |
| X and X2 | `incompatible` | `ROLE-COMBINATION-POSITION-MISMATCH` |
| Impulse motive position and impulse corrective position | `incompatible` | `ROLE-IMPULSE-MOTIVE-CORRECTIVE-MISMATCH` |
| No explicit approved relationship | `incompatible` | `ROLE-NO-STRUCTURAL-RULE` |

The first connector X is never overwritten or reinterpreted as X2.

### Family Rules

| Pair | Class |
|---|---|
| Same known family | `exact_match` |
| Impulse and diagonal | `compatible` |
| Double three and triple three | `compatible` |
| Flat and zigzag | `context_only` |
| Different non-triangle corrective families | `context_only` |
| Corrective A and another known family | `context_only`, subject to the refined role rule |
| Triangle and a non-triangle family | `incompatible` |
| Motive and corrective families | `incompatible` |
| Unknown family | `incompatible` |

The role and family results are combined conservatively. An incompatibility in either layer excludes the case. A specific compatible role rule can admit an intended analogue such as Zigzag C versus Flat C even though the broader families are context-only.

## Progressive Search Levels

| Level | Meaning |
|---:|---|
| 1 | Exact role, exact family, same Elliott degree. |
| 2 | Exact role, exact family, adjacent Elliott degree. |
| 3 | Compatible role/family, explicitly enabled context comparison, or explicitly enabled far-degree comparison. |
| 4 | Explicitly enabled cross-market analogue using normalized endpoint fields only. |

Every eligible item contains `comparison_level`, `comparison_reason`, `compatibility_class`, the role/family/degree `compatibility_rule`, and exact `matrix_location`.

## Configuration

| Setting | Default | Behavior |
|---|---:|---|
| `same_degree_only` | `false` | When true, adjacent degrees are disabled. |
| `allow_adjacent_degree` | `true` | Same and adjacent degree are eligible by default. |
| `allow_far_degree` | `false` | Far-degree cases require explicit opt-in. |
| `allow_cross_market` | `false` | Cross-market normalized analogues require explicit opt-in. |
| `allow_context_family` | `false` | Context-only structural relationships require explicit opt-in. |

CLI equivalents:

```powershell
python -m elliott_ai experience filter EXPERIENCE_CASE_ID
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --same-degree-only
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --no-adjacent-degree
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --allow-far-degree
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --allow-cross-market
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --allow-context-family
```

## Hard Exclusions

The filter exposes each exclusion with `rule`, `reason`, and `matrix_location` or validation location.

| Category | Rules |
|---|---|
| Identity and lineage | `HARD-SELF-MATCH`, `HARD-SAME-ACTIVE-EPISODE`, `HARD-INACTIVE-EPISODE-VERSION` |
| Human review | `HARD-UNREVIEWED`, `HARD-QUARANTINED` |
| Versioning | `HARD-UNSUPPORTED-EXPERIENCE-SCHEMA`, `DNA-UNSUPPORTED-SCHEMA` |
| DNA integrity | `DNA-MISSING`, `DNA-MISSING-MANDATORY-GROUP`, `DNA-MISSING-SOURCE-HASH`, `DNA-INVALID-HASH` |
| Timing | `DNA-ENDPOINT-ONLY`, `DNA-ENDPOINT-CUTOFF-LEAKAGE`, `DNA-GROUP-CUTOFF-LEAKAGE`, `DNA-LATER-EVIDENCE-LEAKAGE` |
| Provenance | `CURRENT-MANDATORY-PROVENANCE-MISSING`, `HARD-INCOMPATIBLE-MANDATORY-PROVENANCE` |
| Structure | `HARD-UNRESOLVED-ROLE-CLASS`, `HARD-STRUCTURAL-INCOMPATIBILITY` |
| Conservative configuration | `CONFIG-CONTEXT-FAMILY-DISABLED`, `CONFIG-ADJACENT-DEGREE-DISABLED`, `HARD-FAR-DEGREE`, `CONFIG-CROSS-MARKET-DISABLED` |

`HARD-SAME-ACTIVE-EPISODE` means another version of the current candidate's own `market_episode_id`; admitting it would duplicate the same observation and leak revision knowledge. Genuine different reviewed market episodes are the historical comparison corpus. Superseded or otherwise inactive versions of those episodes are excluded separately.

## Soft Metadata

These observations are recorded as `effect: metadata_only_no_score` and never rank a case:

- adjacent degree;
- cross-market context;
- different feed;
- different regime;
- different volatility;
- missing optional indicators;
- different liquidity;
- different venue;
- different derivative type;
- different quote currency.

No missing value is converted to zero.

## Feed Compatibility

A differing feed does not reject an otherwise valid structural comparison. It marks feed-bound groups such as `volume.raw`, `volume_profile`, `funding`, and `basis` as incomparable. A timeframe difference also makes raw EWO, raw MACD, and raw candle duration incomparable. Cross-market Level 4 additionally limits comparison to normalized endpoint fields.

Missing provider, feed identity, or timeframe provenance remains a hard exclusion because comparability cannot be audited without it.

## Filtering Examples

These examples were generated directly from the implemented filter with deterministic test fixtures.

| Current versus historical | Result |
|---|---|
| Minor Wave 1 versus Minor Wave 1 | Eligible, Level 1, `exact_match` |
| Minor Wave 1 versus Intermediate Wave 1 | Eligible, Level 2, `exact_match`, adjacent-degree metadata |
| Double-three Y versus triple-three Y | Eligible, Level 3, `compatible` |
| Equity Wave 1 versus crypto Wave 1 with cross-market opt-in | Eligible, Level 4, normalized groups only |
| Wave 1 versus motive Corrective A, default config | Excluded as disabled `context_only`; eligible at Level 3 with `allow_context_family` |
| Triangle B versus Zigzag B | Excluded as structurally `incompatible` |
| Exact Wave 1 across different feeds | Eligible at Level 1; raw volume/profile/funding/basis marked incomparable |

Example eligible contract:

```json
{
  "eligible": true,
  "excluded": false,
  "comparison_level": 3,
  "compatibility_class": "compatible",
  "comparison_reason": "Level 3: compatible structural roles; same degree.",
  "rule": "STRUCTURAL-FILTER-PASSED",
  "matrix_location": "role[double_three.y][triple_three.y] + family[double_three][triple_three]",
  "missing_groups": ["momentum", "optional_rsi", "volume"],
  "incomparable_groups": [],
  "soft_penalties": []
}
```

Example exclusion contract:

```json
{
  "eligible": false,
  "excluded": true,
  "comparison_level": null,
  "compatibility_class": "incompatible",
  "rule": "HARD-STRUCTURAL-INCOMPATIBILITY",
  "reason": "Triangle legs compare only with the exact same triangle leg role.",
  "matrix_location": "role[triangle.b][zigzag.b] + family[triangle][zigzag]",
  "missing_groups": [],
  "incomparable_groups": []
}
```

## Endpoint-Only Verification

1. `KnowledgeStore._experience_comparison_record()` loads case status and endpoint DNA only.
2. `filter_structurally_comparable_cases()` reads only the `endpoint_dna` key from current and historical records.
3. Injecting different `confirmation_dna` or `outcome_dna` payloads produces byte-for-byte identical filter output.
4. Endpoint DNA containing confirmation, displacement, five-away, origin, X2-Z continuation, or resolved-outcome keys is rejected as later-evidence leakage.
5. Endpoint and feature-group cutoffs are validated before structural comparison.
6. Filtering persists no query or match and creates no `experience_queries` or `experience_matches` table.

## New Tests

The 26 Phase 5A.2 tests cover:

1. Every required explicit role and classifier path.
2. Exhaustive, versioned role matrix construction.
3. Exhaustive family matrix construction.
4. All four compatibility classes.
5. Family exact, compatible, context-only, and incompatible rules.
6. Separate X and X2 semantics.
7. Triangle isolation.
8. All four progressive search levels.
9. Self-match and same-episode exclusion.
10. Adjacent-degree defaults and configuration switches.
11. Far-degree rejection and opt-in.
12. Double-three Y versus triple-three Y.
13. Wave 1 versus Corrective A context-only handling.
14. Cross-market rejection, opt-in, and normalized scope.
15. Stable deterministic identity ordering with no score or rank fields.
16. Unreviewed, quarantined, and inactive-version exclusion.
17. Unsupported experience schema and invalid Pattern DNA hash.
18. Endpoint cutoff leakage.
19. Missing mandatory DNA and provenance.
20. Feed-bound group incomparability.
21. Missing optional indicators as metadata only.
22. Confirmation/outcome payload isolation.
23. Explainability fields on every exclusion.
24. Later-timing keys inside endpoint DNA.
25. Empty accepted-pool cold start.
26. CLI filter and matrix command exposure without persistence commands.

## Verification Results

Command:

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```

Result on 21 July 2026:

| Metric | Count |
|---|---:|
| Tests run | 244 |
| Passed | 244 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |

The focused Phase 5A.2 suite ran 26 tests and passed all 26.

Live database verification:

| Check | Result |
|---|---|
| SQLite integrity | `ok` |
| Foreign-key violations | 0 |
| Experience cases | 0 |
| Pattern DNA rows | 0 |
| Experience reviews | 0 |
| Accepted experience pool | 0 |

Cold start therefore returns no eligible historical experience instead of fabricating an analogue.

## Compatibility Risks

1. The live accepted pool is empty, so real-case filtering will remain unavailable until reviewed Phase 4 outcomes are deliberately converted and human-accepted under Phase 5A.1.
2. Legacy endpoint DNA that identifies a generic Corrective A but lacks its actual internal family is excluded as unresolved. This is intentional and prevents letter-only comparison, but those cases need a reviewed fingerprint revision before use.
3. Future experience or Pattern DNA schema versions are excluded until an explicit compatibility adapter is added. Existing Phase 5A.1 records remain readable.
4. Missing optional market metadata can hide a regime, venue, liquidity, or cross-market distinction. It never becomes zero and never creates a structural invalidation.
5. Feed-mismatched cases remain structurally eligible, so all later phases must honor the emitted `incomparable_groups` and never silently compare raw feed-bound values.
6. The output order is deterministic identity order only. A caller must not reinterpret that order as relevance because Phase 5A.3 ranking does not exist yet.

## Phase Boundary

Phase 5A.2 is complete. No similarity formula, retrieval ranking, historical outcome aggregation, canonical-memory promotion, probability calibration, machine learning, or trading logic was implemented.
