# Historical Experience Population and Review Workflow Completion Report

## Scope

This phase implements a controlled, append-only workflow for discovering historical endpoint candidates, assembling immutable drafts, validating Phase 3/4 references, recording separate human structural and outcome reviews, resolving reviewer disagreement explicitly, and admitting a case to the existing Phase 5A experience pool only through final human acceptance.

It adds no machine learning, prediction, probabilities, automatic Elliott Wave resolution, analogue voting, trade decisions, adaptive weighting, outcome-aware ranking, post-endpoint candle storage, or post-endpoint indicator calculation.

## Changed Files

| File | Change |
|---|---|
| `elliott_ai/experience_workflow.py` | Adds the exact approved SQL, workflow specification, state matrix, source validation, draft and event hashes, structural/outcome review contracts, disagreement rules, chain validation, and text rendering. |
| `elliott_ai/knowledge.py` | Adds migration backup/audit, atomic schema initialization, workflow persistence and replay, discovery, draft assembly, reviews, final acceptance, supersession, pool access, and transaction-aware Phase 5A.1 persistence helpers. |
| `elliott_ai/cli.py` | Adds workflow discovery, draft, validation, review, status, acceptance, rejection, withdrawal, supersession, pool, JSON, and text commands. Existing commands are unchanged. |
| `scripts/test_experience_workflow.py` | Adds 40 workflow, migration, safety, compatibility, CLI, and SQLite tests. |
| `ELLIOTT_AI_README.md` | Documents the workflow, review files, commands, acceptance boundary, and deferred capabilities. |
| `.elliott_ai/elliott_ai.sqlite3` | Adds only the two approved workflow tables and eight indexes. No cases or reviews were populated. |
| `.elliott_ai/backups/elliott_ai.pre_experience_workflow_20260721T213803Z.sqlite3` | Consistent pre-migration SQLite backup. |
| `.elliott_ai/backups/elliott_ai.pre_experience_workflow_20260721T213803Z.sqlite3.migration-audit.json` | Pre/post row counts, integrity, foreign-key results, versions, and restoration policy. |
| `HISTORICAL_EXPERIENCE_WORKFLOW_COMPLETION_REPORT.md` | This report. |

## Versioned Contracts

| Contract | Version |
|---|---|
| Workflow specification | `historical-experience-workflow-1.0.0` |
| Workflow specification hash | `055d145cdbbe3eec842f1af57ca65a28db00844fd9436b9c0a4073f0bba6c20c` |
| Workflow case schema | `historical-experience-workflow-case-1.0.0` |
| Workflow case calculation | `historical-experience-workflow-case-calc-1.0.0` |
| Workflow event schema | `historical-experience-workflow-event-1.0.0` |
| Structural review | `experience-structural-review-1.0.0` |
| Outcome review | `experience-outcome-review-workflow-1.0.0` |
| Final acceptance | `experience-final-acceptance-1.0.0` |
| Phase 5B taxonomy reused | `reviewed-outcome-taxonomy-1.0.0` |
| Phase 5B horizon reused | `reviewed-resolution-window` version `1.0.0` |

## Workflow States

The complete state set is:

```text
discovered
draft
assembled
validation_failed
ready_for_structural_review
structural_review_in_progress
structural_review_accepted
structural_review_rejected
structural_review_needs_revision
structural_review_ambiguous
ready_for_outcome_review
outcome_review_in_progress
outcome_review_accepted
outcome_review_rejected
outcome_review_needs_revision
outcome_review_ambiguous
ready_for_final_acceptance
accepted
rejected
withdrawn
superseded
```

Only `accepted` means final admission to the existing accepted experience pool.

## Transition Table

| From | Permitted destinations |
|---|---|
| Initial | `discovered` |
| `discovered` | `draft`, `rejected`, `withdrawn`, `superseded` |
| `draft` | `assembled`, `validation_failed`, `rejected`, `withdrawn`, `superseded` |
| `assembled` | `validation_failed`, `ready_for_structural_review`, `rejected`, `withdrawn`, `superseded` |
| `validation_failed` | `assembled`, `rejected`, `withdrawn`, `superseded` |
| `ready_for_structural_review` | `structural_review_in_progress`, `rejected`, `withdrawn`, `superseded` |
| `structural_review_in_progress` | the four explicit structural-review result states |
| Structural result states | later structural review, explicit progression where applicable, `rejected`, `withdrawn`, or `superseded` |
| `ready_for_outcome_review` | `outcome_review_in_progress`, renewed structural review, `rejected`, `withdrawn`, `superseded` |
| `outcome_review_in_progress` | the four explicit outcome-review result states |
| Outcome result states | later outcome review, renewed structural review where applicable, explicit progression, `rejected`, `withdrawn`, or `superseded` |
| `ready_for_final_acceptance` | `accepted`, `rejected`, renewed structural/outcome review, `withdrawn`, `superseded` |
| `accepted` | `superseded` |
| `rejected` | `superseded` |
| `withdrawn` | `superseded` |
| `superseded` | none |

The complete machine-readable matrix is returned by `experience workflow-spec`. State is never stored as a mutable current-state column. It is derived by validating and replaying every event in sequence.

## Database Migration

Exactly one additive migration was applied. It created:

1. `experience_workflow_cases`
2. `experience_workflow_events`

No existing table was altered. Every workflow foreign key uses `ON DELETE RESTRICT`. The event table enforces:

- `UNIQUE(workflow_case_id, sequence_number)`;
- `UNIQUE(parent_event_id)` to prevent event-chain branching;
- explicit state and event-kind checks;
- separate nullable outcome and result links that must appear in complete pairs;
- a named human and `human_confirmed = 1` for all review/final-decision events; and
- complete Phase 4 outcome, existing experience case, and existing experience review links for final `accepted` events.

The exact SQL is the `EXPERIENCE_WORKFLOW_MIGRATION_SQL` tuple in `elliott_ai/experience_workflow.py`. It is identical to the approved pre-migration specification.

## Migration Transaction And Rollback

Before DDL, the store used the SQLite backup API to create a consistent copy and recorded a pre-migration integrity snapshot. It then:

1. enabled foreign keys;
2. refused a partial one-table schema;
3. acquired `BEGIN IMMEDIATE`;
4. executed both table and all index statements individually;
5. validated exact columns, indexes, and RESTRICT relationships;
6. ran `integrity_check` and `foreign_key_check` inside the transaction; and
7. committed once.

Any exception before commit rolls back all DDL. Reopening the migrated store validates the schema and performs no migration or backup.

Rollback requires closing Elliott AI, preserving the migrated database and audit artifacts, and restoring the recorded backup only after explicit destructive approval. No automatic destructive rollback was added.

## Hashing And Concurrency

Hashes are SHA-256 over UTF-8 canonical JSON with ASCII escaping, sorted keys, compact separators, and non-finite numbers rejected.

- Source-pair hash: endpoint snapshot ID/hash plus fingerprint integer ID/hash and identity version.
- Material-episode hash: outcome-independent feed, symbol, timeframe, degree, endpoint, role, family, and snapshot cutoff identity.
- Draft hash: complete immutable draft excluding only `content_hash`.
- Review hash: complete typed review block excluding only `review_hash`.
- Event hash: identity, parent, sequence, transition, actor/reviewer, source/result links, versions, typed evidence, and timestamp, excluding only `event_id` and `content_hash`.

Workflow writes use `BEGIN IMMEDIATE`. The next sequence and parent are read under the write lock. The database unique constraints reject a duplicate sequence or second child for one parent.

## Candidate Discovery

Discovery scans only root Phase 4 endpoint snapshots and their immutable Phase 3 fingerprint references. It does not read reviewed outcomes. It reports:

- source IDs and hashes;
- symbol, timeframe, degree, endpoint role, and material episode;
- source-pair duplicate state;
- validation status and hard exclusions; and
- whether a draft already exists.

Discovery never asserts structural correctness and never creates or accepts a case.

## Draft Assembly And Validation

A draft records:

- workflow and market-episode identity;
- correction case, endpoint snapshot, and fingerprint references;
- symbol, timeframe, degree, endpoint, role, role class, family, feed, and cutoffs;
- validation checks, hard exclusions, warnings, and missing optional fields;
- exact-pair and material-episode duplicate evidence; and
- deterministic content hash.

Validation checks canonical row/JSON hashes, root-snapshot status, source linkage, symbol, timeframe, degree, endpoint, role, family, provider, venue, market type, feed identity, schema versions, and cutoff ordering. Passing validation advances only to `ready_for_structural_review`.

## Structural Review

Every structural review is a separate named-human event. Required fields include role, degree, endpoint and family confirmation, Elliott-rule notes, Fibonacci, RSI, volume, channel and multi-timeframe notes, ambiguity notes, alternatives, and rejection reason codes.

Acceptance requires explicit `confirmed` values for role, degree, endpoint, and family. Indicators remain evidence only and cannot invalidate a structurally valid count by themselves.

Decisions are:

- `accepted`
- `rejected`
- `needs_revision`
- `structurally_ambiguous`

## Outcome Review

Outcome review uses the existing Phase 5B taxonomy and reviewed-resolution horizon. It records endpoint linkage, structural aftermath, confirmation/invalidation behavior, unresolved state, data completeness, feed continuity, reviewer limitations, notes, and rejection reasons.

Outcome acceptance requires the current accepted Phase 4 outcome/review, confirmed endpoint linkage, and the taxonomy category mapped from the reviewed Phase 4 hypothesis. Favorable or unfavorable historical movement is never an acceptance or rejection rule.

No missing price, RSI, volume, or post-endpoint measurements are fabricated.

## Reviewer Disagreement

Multiple immutable reviews are preserved. A contradictory accepted/rejected review creates `review_disagreement` and an ambiguous stage state. Final acceptance is blocked while any disagreement event remains unresolved.

Resolution requires a later named-human `disagreement_resolution` event whose review block references every unresolved conflicting event through `resolves_event_ids`. No majority vote, averaging, senior authority, or automatic adjudication exists.

## Final Acceptance

Final acceptance requires all of the following:

1. valid, contiguous, hash-correct workflow event chain;
2. no unresolved disagreement;
3. valid workflow draft hash and row projection;
4. current valid Phase 3/4 hashes, provenance, source linkage, and cutoffs;
5. accepted structural review;
6. accepted workflow outcome review;
7. current accepted Phase 4 outcome and review;
8. exact workflow outcome-event linkage to that current revision;
9. `ready_for_final_acceptance`; and
10. explicit named-human final acceptance with rationale.

One `BEGIN IMMEDIATE` transaction then creates or verifies the existing Phase 5A.1 experience case and three DNA rows, appends the existing human acceptance review, and appends the final workflow event linking both IDs. Any failure rolls the entire transaction back.

## Duplicate And Supersession Rules

- Exact snapshot/fingerprint pairs are unique and idempotent.
- Source-pair hashes are unique.
- Materially identical episodes are reported but not merged or deleted.
- A replacement must explicitly name `supersedes_workflow_case_id` and describe the same market episode.
- Supersession appends an event linking the replacement. It does not delete history.
- Rejected, withdrawn, and superseded workflow cases are excluded from the workflow accepted pool.

## CLI

```powershell
python -m elliott_ai experience workflow-spec --format text
python -m elliott_ai experience workflow-candidates --include-invalid --format json
python -m elliott_ai experience draft CORRECTION_CASE_ID
python -m elliott_ai experience validate WORKFLOW_CASE_ID
python -m elliott_ai experience workflow-inspect WORKFLOW_CASE_ID --format text --explain
python -m elliott_ai experience review-structure WORKFLOW_CASE_ID --decision accepted --review-file structural_review.json --reviewer "Parwa"
python -m elliott_ai experience review-outcome WORKFLOW_CASE_ID --decision accepted --review-file outcome_review.json --reviewer "Parwa"
python -m elliott_ai experience acceptance-status WORKFLOW_CASE_ID
python -m elliott_ai experience accept WORKFLOW_CASE_ID --reviewer "Parwa" --notes "Final explicit review"
python -m elliott_ai experience reject WORKFLOW_CASE_ID --reviewer "Parwa" --reason source_not_suitable --notes "Reason"
python -m elliott_ai experience withdraw WORKFLOW_CASE_ID --reviewer "Parwa" --notes "Reason"
python -m elliott_ai experience supersede OLD_ID --replacement NEW_ID --actor "Parwa"
python -m elliott_ai experience pool --state all --format text
```

No batch-acceptance command exists.

## Tests

New focused workflow suite:

```text
python -m unittest scripts.test_experience_workflow
Ran 40 tests
OK
```

Phase 3 through workflow regression suite:

```text
Ran 248 tests
OK
```

Complete project suite:

```text
python -m unittest discover -s scripts -p "test_*.py"
Ran 377 tests
OK
```

Exact result:

| Metric | Count |
|---|---:|
| Passed | 377 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |

The tests prove that technical validation does not accept a case, outcome wording cannot accept or reject it, retrieved analogues cannot mutate workflow review state, ambiguity blocks admission, outcome changes do not rewrite structural reviews, source records remain immutable, and every accepted case has an explicit human final event.

## Live SQLite Audit

| Check | Result |
|---|---|
| Migration count | 1 |
| Application table count | 31 |
| New tables | 2 |
| `PRAGMA integrity_check` | `ok` |
| Foreign-key violations | 0 |
| SQLite `user_version` | 0, unchanged |
| Workflow cases | 0 |
| Workflow events | 0 |
| Existing experience cases | 0 |
| Existing experience reviews | 0 |
| Resolved outcomes | 0 |
| Outcome reviews | 0 |

The empty accepted pool remains valid. No production case, outcome, reviewer, candle, or review was fabricated.

## Compatibility

Existing Phase 3, Phase 4, Phase 5A, Phase 5B, experience-case, Pattern DNA, and experience-review tables were not altered. Existing JSON and CLI behavior remain readable. The old direct Phase 5A.1 commands remain available for backward compatibility; the new population workflow is the controlled path for creating newly reviewed production experience.

Phase 5A retrieval and Phase 5B outcome evidence pass unchanged against cases accepted through this workflow.

## Limitations And Deferred Work

Deferred to a separately approved phase:

- immutable post-endpoint candle-series storage or references;
- fixed bar or calendar observation horizons;
- deterministic MFE, MAE, drawdown, retracement, extension, and aftermath measurements;
- post-endpoint RSI, EWO, MACD, volume, volatility, channel, and trendline calculations;
- reviewer identity registry or authentication;
- persisted user interface beyond CLI JSON/text;
- batch validation beyond deterministic listing;
- any statistical learning, calibration, prediction, or recommendation system.

## Completion Confirmation

Historical Experience Population and Review Workflow is complete within the approved two-table scope. No automatic acceptance, machine learning, prediction, probability, automatic wave resolution, analogue voting, trade decision, outcome-aware ranking, fabricated evidence, or post-endpoint market-data persistence was added.
