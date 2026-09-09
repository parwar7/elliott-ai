# Phase 4 Completion Report

## Scope

Phase 4 implements correction-state tracking, decision-time hypotheses, and reviewed historical outcome resolution. It does not train a model, assign feature weights, generate calibrated probabilities, change Elliott hard rules, make RSI mandatory, or begin Phase 5.

## Changed Files

- `elliott_ai/correction_state.py`: new immutable state engine, cutoff filtering, event detectors, evidence timing, hypothesis preservation, hard invalidation, and human outcome records.
- `elliott_ai/knowledge.py`: additive Phase 4 migration, automatic pre-migration backup, append-only persistence, lineage queries, review/rejection/revision, and reviewed export.
- `elliott_ai/reporting.py`: optional correction-state section and full lineage report.
- `elliott_ai/cli.py`: correction-case audit and human-review commands.
- `scripts/test_correction_state.py`: 38 deterministic Phase 4 tests.
- `.elliott_ai/elliott_ai.sqlite3`: additive schema migration only; existing records were preserved.
- `.elliott_ai/backups/elliott_ai.pre_phase4_20260721.sqlite3`: pre-migration backup.

## State Machine

Normal progress is:

`structural_terminal_candidate` -> `structurally_complete` -> `terminal_evidence_supportive` -> `reversal_displacement_observed` -> `five_wave_move_away_candidate` -> `corrective_retracement_candidate` -> `origin_hold_observed` -> `next_motive_leg_candidate` -> `structurally_confirmed`

Additional states are `unresolved`, `invalidated`, and `superseded`.

- A local C, Y, or Z can be structurally complete without confirming completion of the whole correction.
- Indicator evidence can advance a candidate to supportive evidence, but cannot invalidate it.
- A five-away candidate requires six valid pivots or five anchored children with hard-rule validation. It creates both Wave 1 and Wave A candidates.
- A corrective retracement preserves Wave 1 and Wave A.
- Subsequent validated motive structure supports Wave 1 but does not automatically invalidate Wave A.
- Only a hard structural rule, explicit crossed price level, impossible later sequence, or explicit human outcome review can remove an alternative.
- X2-Z child structure confirms continuation and invalidates the interpretation that the prior Y ended the entire correction.
- Human review creates a new child snapshot. It never edits a decision-time snapshot.

Every transition records the old and new state, timed triggering/contradictory/unavailable evidence references, structural rule, price invalidation level, cutoff, source run, calculation version, timestamp, and provisional/confirmed status.

## Event Detectors

The deterministic post-terminal layer records:

- structural completion;
- displacement away from the endpoint;
- channel break;
- local structure break;
- structure-verified five-wave move away;
- retracement and corrective-family verification;
- protected-origin hold or return;
- subsequent validated motive candidate;
- explicit price-level crossings;
- X2-Z continuation.

All source candles are filtered at the snapshot cutoff before any event is evaluated. RSI, volume, EWO, and MACD are evidence only.

## Database Tables

| Table | Purpose |
|---|---|
| `correction_schema_versions` | Registers decision-snapshot and outcome schema versions. |
| `correction_cases` | Stable endpoint identity and source/feed context. |
| `hypothesis_snapshots` | Immutable parent-linked decision snapshots. |
| `hypothesis_states` | Per-hypothesis stage and status for each snapshot. |
| `hypothesis_transitions` | Machine-readable append-only transition audit. |
| `resolved_outcomes` | Versioned future historical interpretations, separate from `degree_resolutions`. |
| `outcome_reviews` | Human approval, rejection, and revision history. |

Stable IDs, canonical content hashes, foreign keys, deduplication, unresolved cases, multiple snapshots, and outcome revisions are supported.

## Migration Audit

Backup:

`C:\Users\Parwa\Documents\Codex\2026-06-27\listen-there-is-an-indicator-in\.elliott_ai\backups\elliott_ai.pre_phase4_20260721.sqlite3`

Both backup and migrated database passed `PRAGMA integrity_check`.

| Record type | Before | After |
|---|---:|---:|
| Documents | 59 | 59 |
| Chunks | 696 | 696 |
| Analysis runs | 18 | 18 |
| Degree resolutions | 3 | 3 |
| Correction cases | 0 | 0 |
| Resolved outcomes | 0 | 0 |

No historical correction case or outcome was fabricated or backfilled.

## Example Lineage

This is a deterministic synthetic test lineage. It was not stored in the live database.

| # | Cutoff | Snapshot | Parent | Stage | Reviewed |
|---:|---|---|---|---|---|
| 1 | 2026-01-01 | `snapshot_289ee93cfdd86db8c149d7e2` | none | `structurally_complete` | No |
| 2 | 2026-01-03 | `snapshot_05e80ff17be8d2f8c9adea81` | snapshot 1 | `reversal_displacement_observed` | No |
| 3 | 2026-01-07 | `snapshot_97a8243929509b481c10ede9` | snapshot 2 | `five_wave_move_away_candidate` | No |
| 4 | 2026-01-10 | `snapshot_ef5a4f40600a664c2f6821a3` | snapshot 3 | `origin_hold_observed` | No |
| 5 | 2026-01-12 | `snapshot_d8f02b6a2a01aae635e0f0d7` | snapshot 4 | `next_motive_leg_candidate` | No |
| 6 | 2026-01-31 | `snapshot_1887276fb11247c2bf756bad` | snapshot 5 | `structurally_confirmed` | Yes |

The endpoint snapshot content hash remained:

`8e58618920ea8a1f4b9a49b2d1b6982ce53acda5add398f35aac2a7204f8ba76`

after all later snapshots and the human review were created.

## Required Outcome Examples

1. **WXY resolved as complete:** `correction_complete` is selected after later displacement, a structure-verified five-away move, a corrective retracement, origin hold, and reviewed future structure. Larger W, larger A, continued correction, and X2-Z are rejected with human reasons.
2. **WXY resolved as larger W:** `larger_wave_w_complete` is selected after a later corrective X and Y demonstrate that the local WXY was the first corrective family at the larger degree.
3. **WXY resolved as larger A:** `larger_wave_a_complete` is selected after later structure supplies a compatible larger B and motive C.
4. **Y continues into X2-Z:** a distinct X2 makes `correction_complete` structurally invalid at the prior Y; `triple_three_continuation` advances to confirmed while larger W and larger A remain until separately invalidated or reviewed.
5. **Wave 1 versus Wave A unresolved:** a verified five-away move creates both candidates. A corrective retracement and origin hold preserve both. Later motive acceleration supports Wave 1 but does not invalidate Wave A by itself.

## Evidence Timing Demonstration

- Snapshot 1 contains only `available_at_endpoint` evidence.
- Snapshots 2-5 add `available_after_endpoint` evidence at their own cutoffs.
- Resolution-time future structure is stored in the outcome record.
- Snapshot 6 adds `human_review_only` evidence.
- No post-terminal or review evidence appears in snapshot 1.

## CLI Operations

```powershell
python -m elliott_ai correction-cases --status unresolved
python -m elliott_ai correction-lineage CASE_ID
python -m elliott_ai correction-evidence SNAPSHOT_ID
python -m elliott_ai correction-report CASE_ID --output correction_report.md
python -m elliott_ai resolve-correction CASE_ID --snapshot SNAPSHOT_ID --file outcome.json --reviewer NAME --resolution-cutoff TIMESTAMP
python -m elliott_ai reject-correction OUTCOME_ID --reviewer NAME --reason "Reason"
python -m elliott_ai revise-correction CASE_ID --snapshot SNAPSHOT_ID --file outcome.json --reviewer NAME --resolution-cutoff TIMESTAMP
python -m elliott_ai export-corrections --output reviewed_corrections.json
```

An LLM proposal is not treated as reviewed merely because it is stored. A named human review action is required to resolve a case.

## Verification

Command:

```powershell
python -m unittest discover -s scripts -p "test_*.py" -q
```

Result: **191 passed, 0 failed, 0 errors**.

The 38 Phase 4 tests cover cutoff immutability, extreme future-candle attacks, fingerprint hash preservation, verified-pivot safeguards, all requested WXY outcomes, X2-Z, Wave 1 versus Wave A, indicator non-invalidation, explicit price invalidation, unresolved lower-timeframe data, lineage order, outcome rejection/revision, report timing, migration backup, no backfill, and old-record readability.

## Compatibility Risks

- Existing tables and JSON contracts are unchanged; all Phase 4 tables and report fields are additive.
- Existing 18 runs remain ordinary runs until a real endpoint case is explicitly created.
- Callers creating snapshots must supply parseable cutoffs, timed evidence, original fingerprint hashes, and actual pivots/children for five-wave claims.
- Older code that only uses `X` remains unchanged; Phase 4 consumes the Phase 1 X/X2 semantics without modifying stored data.
- Outcome confidence is descriptive human metadata, not a calibrated probability.
- No reviewed outcome affects future analysis or scoring in Phase 4.

Phase 4 is complete. Phase 5 has not started.
