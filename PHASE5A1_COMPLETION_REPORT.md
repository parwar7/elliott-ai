# Phase 5A.1 Completion Report

## Scope

Phase 5A.1 implements experience storage, Pattern DNA projection, append-only human review, quality assignment, versioned tags, and audit export. It does not implement Phase 5A.2 filtering, Phase 5A.3 similarity, Phase 5A.4 reporting aggregation, Phase 5B machine learning, probabilities, trading decisions, or automatic historical backfill.

## Changed Files

| File | Change |
|---|---|
| `elliott_ai/experience.py` | New deterministic episode/case identities, eligibility guardrails, endpoint/confirmation/outcome DNA, rule-based quality, reviews, and tags. |
| `elliott_ai/knowledge.py` | Additive migration, backup/audit, six Phase 5A.1 tables, source resolution, immutable persistence, review/tag lineage, active-pool logic, inspection, and export. |
| `elliott_ai/cli.py` | Adds the nested `experience` command family. |
| `scripts/test_experience_engine.py` | Adds 27 Phase 5A.1 tests. |
| `ELLIOTT_AI_README.md` | Documents Phase 5A.1 behavior and CLI use. |
| `.elliott_ai/elliott_ai.sqlite3` | Additive schema migration only; no experience was backfilled. |
| `.elliott_ai/backups/elliott_ai.pre_phase5a1_20260721T161521Z.sqlite3` | Exact pre-migration backup. |
| `.elliott_ai/backups/elliott_ai.pre_phase5a1_20260721T161521Z.sqlite3.migration-audit.json` | Recorded pre/post counts, integrity, foreign-key status, and restoration policy. |

## Identity Formulas

All hashes are SHA-256 over ASCII canonical JSON with sorted keys, compact separators, finite numbers only, and UTC timestamps normalized to seconds.

### Market Episode

`market_episode_id = "market_episode_" + first32(SHA256(canonical_json(fields)))`

The fields are:

```text
identity_version
provider
venue
feed_identity
symbol
timeframe
elliott_degree
endpoint_timestamp
candidate_role
parent_pattern_family
```

Outcome IDs, outcome revisions, snapshots, fingerprint versions, experience reviews, and endpoint price are deliberately excluded. Therefore, a revised interpretation or fingerprint remains another version of the same observed market event.

### Experience Case Version

`experience_case_id = "experience_case_" + first32(SHA256(canonical_json(fields)))`

The fields are:

```text
identity_version
market_episode_id
endpoint_snapshot_id and hash
fingerprint_identity_hash and content_hash
outcome_id, content_hash, and revision
outcome_review_id and content_hash
experience schema version
Pattern DNA schema and calculation versions
```

The same source set is idempotent. A reviewed outcome revision or fingerprint revision produces a new case version under the same market episode. The active version is the highest stored version; prior versions are derived as `superseded` without editing them.

## Eligibility Contract

A candidate is assembled only when all of these pass:

1. The endpoint snapshot is the immutable root snapshot and is not a review snapshot.
2. The original Phase 3 fingerprint is stored and referenced by the endpoint snapshot.
3. The latest Phase 4 outcome revision has a current `approved` or `revised` review.
4. Correction case, snapshot, outcome, and outcome review share the same source IDs.
5. Symbol, provider, feed identity, timeframe, Elliott degree, candidate role, and parent family agree.
6. Endpoint timestamps agree exactly unless an explicit non-negative tolerance is supplied.
7. Source schema and calculation versions are supported.
8. Every canonical source hash validates.
9. Fingerprint candles and evidence do not occur after their declared endpoint cutoff.

The validator never repairs a mismatch. Identity/reference mismatches are rejected. Unsupported versions and cutoff leakage are quarantined with machine-readable reason codes.

## Database Tables

| Table | Purpose |
|---|---|
| `experience_schema_versions` | Registers the experience-case and Pattern DNA contracts. |
| `experience_cases` | Immutable episode versions and source references. `supersedes_case_id` points from a new version to its predecessor. |
| `pattern_dna` | Exactly one endpoint, confirmation, and resolved-outcome DNA record per case version. |
| `experience_reviews` | Append-only accept, reject, quarantine, revise, quality, and rationale history with parent review and source outcome-review references. |
| `experience_tags` | Versioned namespaced tag definitions. |
| `experience_case_tags` | Append-only tag add/remove actions with revisions and parent assignments. |

No `experience_queries` or `experience_matches` table or retrieval logic was activated.

## Pattern DNA

Schema: `pattern-dna-1.0.0`

Calculation version: `pattern-dna-projection-1.0.0`

Every feature group records `status`, `values`, `units`, `source_references`, `calculation_version`, `cutoff`, `unavailable_reason`, and `incomparable_reason`. Missing values remain JSON `null`.

### Endpoint DNA

Endpoint DNA is a normalized projection of the original Phase 3 fingerprint. It contains source identity, structure, price shape, duration, available pivot anchors, Fibonacci references, channel behavior, EWO/MACD, volume, volatility, optional RSI, market context, data quality, comparability, and evidence references.

It calls no RSI, EWO, MACD, ATR, volume, price, divergence, or channel calculator. It contains no displacement, five-away, retracement, origin-hold, resolution, X2-Z, or human-review event.

### Confirmation DNA

Confirmation DNA reads only explicit Phase 4 `post_terminal_events` and transitions. It supports:

```text
displacement
channel break
local structure break
five-away candidate and structure-verification status
corrective retracement candidate and structure status
origin hold
origin invalidation
next motive candidate
X2-Z continuation
```

Each event stores its observation time, earliest snapshot cutoff, delay in candles and clock time, source snapshot/hash, transition references, verification state, and original details. Missing events stay unavailable. Final outcome prose is never parsed to invent an event.

### Resolved-Outcome DNA

Outcome DNA references the reviewed Phase 4 outcome and its review action. It preserves the selected hypothesis, interpretation, rejected alternatives and reasons, supporting future structure, outcome revision, reviewer, review status, and resolution cutoff. Creating it does not modify endpoint or confirmation DNA.

## Review and Quality Policy

Persisting a candidate changes its storage state from `assembled_candidate` to `pending_experience_review`. It does not accept it.

Only `experience review --action accept` or an accepting review revision with a named reviewer and `--human-confirmed` can enter the accepted pool. A model recommendation with no human confirmation is rejected by the API.

Current state and quality are derived by replaying immutable review lineage. A revision points to the current parent review and supersedes it semantically; no row is overwritten.

Quality statuses are `high`, `medium`, `low`, and `quarantined`. The proposal records structural-anchor completeness, fingerprint completeness, feed consistency, timeframe completeness, review agreement, cutoff safety, and optional-feature gaps. Optional RSI or other optional gaps are informational and do not create a predictive score. Profitability and market return are not used.

## Synthetic Examples

These examples were generated in a temporary test database. They were not inserted into the live database.

### Assembled Candidate

```json
{
  "state": "assembled_candidate",
  "market_episode_id": "market_episode_84724b10ec4f0ce7765f84d102075c29",
  "experience_case_id": "experience_case_1c1e4d9d7199783306cff6dbea4a33c0",
  "proposed_quality": "high",
  "dna_kinds": ["endpoint", "confirmation", "resolved_outcome"]
}
```

### Human-Accepted Case

```json
{
  "state": "accepted",
  "quality_status": "high",
  "active_version": true,
  "accepted_pool_eligible": true,
  "reviewer": "Parwa",
  "human_confirmed": true
}
```

### Quarantined Cutoff Mismatch

```json
{
  "state": "quarantined",
  "reason_codes": ["cutoff_leakage"],
  "message": "Endpoint fingerprint includes information later than the candidate endpoint."
}
```

### Outcome Revision

```json
{
  "same_market_episode": true,
  "old_case_id": "experience_case_1c1e4d9d7199783306cff6dbea4a33c0",
  "old_state": "superseded",
  "new_case_id": "experience_case_78382cff6f2cb5d60438dd830b46c406",
  "new_version": 2,
  "new_state": "pending_experience_review",
  "supersedes": "experience_case_1c1e4d9d7199783306cff6dbea4a33c0"
}
```

Version 1 is immediately excluded from the accepted pool. Version 2 requires a new explicit human experience review.

### Timing Separation

Endpoint DNA referenced fingerprint hash:

`b09fdb1100b1bf639c2694f452830b2ab8501a604165173d24cd7b80de944892`

Confirmation DNA separately recorded a channel break observed at `2026-02-12T00:00:00+00:00`, first available in the `2026-02-15T00:00:00+00:00` snapshot, after 6 candles and 259,200 seconds. Outcome DNA separately recorded `correction_complete`.

After later evidence and a revised outcome were stored, the original endpoint DNA row and hash remained byte-for-byte unchanged.

### Duplicate Sources

Repeating candidate creation returned `inserted: false` and the same case ID. Two outcome revisions produced two stored case versions but export reported one market episode and one active observation.

## Tags

Tags use `namespace:value`, including namespaces such as `canonical`, `structural`, `market`, `regime`, `data-quality`, `anomaly`, and `reviewer`. Removing a tag appends a `remove` action linked to the prior `add`; it never deletes either row.

## CLI

```powershell
python -m elliott_ai experience candidates
python -m elliott_ai experience create CORRECTION_CASE_ID
python -m elliott_ai experience inspect EXPERIENCE_CASE_ID
python -m elliott_ai experience review EXPERIENCE_CASE_ID --action accept --reviewer "Parwa" --rationale "Reviewed" --human-confirmed
python -m elliott_ai experience revise-review EXPERIENCE_CASE_ID --parent-review REVIEW_ID --action accept --reviewer "Parwa" --rationale "Revised" --human-confirmed
python -m elliott_ai experience quality EXPERIENCE_CASE_ID --status medium --reviewer "Parwa" --rationale "Reason"
python -m elliott_ai experience tag EXPERIENCE_CASE_ID --tag market:equity --action add --actor "Parwa"
python -m elliott_ai experience dna EXPERIENCE_CASE_ID --kind confirmation
python -m elliott_ai experience export --output experience.json --accepted-only
```

Phase 4 outcome review and Phase 5A experience review are distinct commands and distinct tables.

## Migration Audit

Live database:

`C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in\.elliott_ai\elliott_ai.sqlite3`

Backup:

`C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in\.elliott_ai\backups\elliott_ai.pre_phase5a1_20260721T161521Z.sqlite3`

Audit:

`C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in\.elliott_ai\backups\elliott_ai.pre_phase5a1_20260721T161521Z.sqlite3.migration-audit.json`

Pre- and post-migration `PRAGMA integrity_check` returned `ok`; both foreign-key violation counts were `0`. Existing counts remained 59 documents, 696 chunks, 18 analysis runs, 3 degree resolutions, 0 fingerprints, 0 correction cases, and 0 reviewed outcomes.

After migration, live counts are 0 experience cases, 0 Pattern DNA records, 0 experience reviews, and 0 tag actions. `experience candidates` returns `[]`; the live accepted pool is empty.

To restore, first close every Elliott AI process, preserve the migrated database under a diagnostic filename, and then copy the recorded backup to the live database path. The migration itself is idempotent, so reopening the current database does not create another backup or duplicate schema rows.

## Verification

Focused suite:

```text
python -m unittest scripts.test_experience_engine
Ran 27 tests
OK
```

Complete suite:

```text
python -m unittest discover -s scripts -p "test_*.py"
Ran 218 tests
OK
```

Exact result: **218 passed, 0 failed, 0 errors**.

## Compatibility Risks

1. Existing tables, records, RSI policy, indicator calculations, hard Elliott rules, and JSON readers are unchanged; all Phase 5A.1 storage is additive.
2. Eligibility is intentionally strict. Legacy sources with unknown provider/feed, incomplete fingerprint hashes, unsupported schema versions, or mismatched endpoint timestamps will be rejected or quarantined rather than repaired.
3. Endpoint alignment tolerance defaults to zero. A tolerance must be supplied explicitly and is recorded in eligibility evidence.
4. When a new outcome/fingerprint version is created, the old accepted experience version becomes superseded immediately. The new active version remains pending until separately accepted by a human.
5. `high`, `medium`, and `low` are transparent data-quality labels, not probabilities, similarity weights, profitability ratings, or trading signals.
6. Phase 5A.1 stores reviewed experience but does not retrieve similar cases into analysis. That remains outside this approved phase.

Phase 5A.1 is complete. No Phase 5A.2, 5A.3, 5A.4, or 5B work was started.
