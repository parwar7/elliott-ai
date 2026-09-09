# Phase 11 Manual Shadow Workflow

This interface is manual, default-off, and append-only. It does not fetch
candles, schedule work, alter the active Elliott pipeline, or take trades.

## Setup

Run from the repository root in PowerShell:

```powershell
$Python = "python"
$Database = ".elliott_ai\elliott_ai.sqlite3"
$Base = @("-m", "elliott_ai", "--database", $Database, "phase11-shadow")
```

Global options must appear before `phase11-shadow`. Replace every angle-bracket
placeholder with an exact immutable ID, hash, UTC timestamp, or file path. A
model-backed command without `--allow-model-call` returns a plan. Omitting
`--commit` runs a preview and writes no database row or checkpoint.

## 1. Start Shadow Analysis

`[MODEL CALL] [WRITE]`

```powershell
$Start = (& $Python @Base start `
  --analysis-run-id <RUN_ID> `
  --degree-resolution-id <RESOLUTION_ID> `
  --cutoff <YYYY-MM-DDTHH:MM:SS+00:00> `
  --executed-at <YYYY-MM-DDTHH:MM:SS+00:00> `
  --provider openai `
  --model <MODEL_ID> `
  --shadow --blind --allow-model-call --commit) | ConvertFrom-Json

$Workflow = $Start.workflow_id
$Checkpoint = $Start.checkpoint.content_hash
$ProposalHash = $Start.result.proposal.content_hash
$Candidate = $Start.result.proposal.proposed_selected_candidate_id
```

There is no `latest` fallback. The run, resolution, and cutoff must match.

## 2. Approve and Freeze Forecast

Create `claims.json` with the selected candidate ID in every claim:

```json
{
  "target_claims": [{
    "claim_id": "target-1",
    "hypothesis_id": "<CANDIDATE_ID>",
    "target_low": 100.0,
    "target_high": 110.0,
    "direction": "up",
    "timeframe": "daily",
    "price_basis": "ohlc",
    "rationale": "Human-defined frozen target.",
    "evaluation_basis": "intrabar_touch",
    "evidence_ids": [],
    "source_wave_ids": [],
    "schema_version": "forecast-target-claim-1.0.0"
  }],
  "invalidation_claims": [{
    "claim_id": "invalidation-1",
    "hypothesis_id": "<CANDIDATE_ID>",
    "operator": "at_or_below",
    "condition": "Price reaches or breaches the frozen level.",
    "timeframe": "daily",
    "price_basis": "ohlc",
    "price_level": 80.0,
    "evaluation_basis": "intrabar_touch_or_breach",
    "evidence_ids": [],
    "source_wave_ids": [],
    "schema_version": "forecast-invalidation-claim-1.0.0"
  }],
  "confirmation_claims": [],
  "expected_completion_windows": [{
    "window_id": "window-1",
    "hypothesis_id": "<CANDIDATE_ID>",
    "start_utc": "<CUTOFF_UTC>",
    "end_utc": "<HORIZON_END_UTC>",
    "timeframe": "daily",
    "start_rule": "First completed post-cutoff candle.",
    "end_rule": "Frozen operator horizon.",
    "rationale": "Human-defined timing window.",
    "schema_version": "forecast-completion-window-1.0.0"
  }]
}
```

`[WRITE] [HUMAN DECISION]`

```powershell
$Approved = (& $Python @Base approve-forecast $Workflow `
  --checkpoint-hash $Checkpoint `
  --proposal-hash $ProposalHash `
  --confirm-proposal-hash $ProposalHash `
  --selected-candidate-id $Candidate `
  --human-actor "human:<REVIEWER>" `
  --approved-at <UTC_TIMESTAMP> `
  --direction up `
  --claims-file .\claims.json `
  --approval-note "Human reviewed the frozen candidate and typed claims." `
  --commit) | ConvertFrom-Json

$Checkpoint = $Approved.checkpoint.content_hash
```

Hard-rule-invalid candidates and mismatched proposal hashes are rejected.

## 3. Register Later Observations

Prepare `observations.json` using the complete normalized Phase 11B
`ForecastObservationSet` contract and its canonical hash. It must reference the
frozen forecast and preserve feed, timeframe, session, adjustment, price basis,
candle closure, cutoff, and horizon compatibility.

`[WRITE]`

```powershell
$Observed = (& $Python @Base register-observations $Workflow `
  --checkpoint-hash $Checkpoint `
  --observation-file .\observations.json `
  --accepted-at <UTC_TIMESTAMP> `
  --commit) | ConvertFrom-Json

$Checkpoint = $Observed.checkpoint.content_hash
```

This command never fetches market data.

## 4. Evaluate Deterministically

`[WRITE] [NO MODEL]`

```powershell
$Evaluated = (& $Python @Base evaluate $Workflow `
  --checkpoint-hash $Checkpoint `
  --evaluated-at <UTC_TIMESTAMP> `
  --commit) | ConvertFrom-Json

$Checkpoint = $Evaluated.checkpoint.content_hash
```

Optional lower-timeframe Phase 11B files may be repeated with
`--lower-timeframe-observation-file <PATH>`. The command cannot call a model.

## 5. Draft and Record Outcome Review

`[MODEL CALL] [WRITE]`

```powershell
$DraftReview = (& $Python @Base draft-review $Workflow `
  --checkpoint-hash $Checkpoint `
  --forecast-id $Evaluated.result.forecast_id `
  --forecast-hash $Evaluated.result.forecast_hash `
  --observation-set-id $Evaluated.result.observation_set_id `
  --observation-set-hash $Evaluated.result.observation_set_hash `
  --evaluation-id $Evaluated.result.evaluation_id `
  --evaluation-hash $Evaluated.result.evaluation_hash `
  --drafted-at <UTC_TIMESTAMP> `
  --provider openai --model <MODEL_ID> `
  --shadow --allow-model-call --commit) | ConvertFrom-Json

$Checkpoint = $DraftReview.checkpoint.content_hash
$ReviewDraft = $DraftReview.result.outcome_review_draft
```

Create `diagnoses.json` as an explicit JSON array of Phase 11C
`FailureDiagnosis` records. Do not treat the advisory draft as human approval.

`[WRITE] [HUMAN DECISION]`

```powershell
$Review = (& $Python @Base record-review $Workflow `
  --checkpoint-hash $Checkpoint `
  --draft-id $ReviewDraft.draft_id `
  --draft-hash $ReviewDraft.content_hash `
  --evaluation-hash $Evaluated.result.evaluation_hash `
  --reviewed-hypothesis-id $ReviewDraft.reviewed_hypothesis_id `
  --human-actor "human:<REVIEWER>" `
  --reviewed-at <UTC_TIMESTAMP> `
  --decision approved `
  --diagnoses-file .\diagnoses.json `
  --evidence-reference-id <VERIFIED_REFERENCE_ID> `
  --notes "Human-reviewed diagnosis." `
  --commit) | ConvertFrom-Json

$Checkpoint = $Review.checkpoint.content_hash
```

## 6. Draft, Record, and Activate a Lesson

This path is available only for an eligible failed or partial outcome with an
approved or revised human review.

`[MODEL CALL] [WRITE]`

```powershell
$DraftLesson = (& $Python @Base draft-lesson $Workflow `
  --checkpoint-hash $Checkpoint `
  --forecast-id $Review.result.forecast_id `
  --forecast-hash $Review.result.forecast_hash `
  --evaluation-id $Review.result.evaluation_id `
  --evaluation-hash $Review.result.evaluation_hash `
  --review-id $Review.result.human_review_id `
  --review-hash $Review.result.human_review_hash `
  --proposed-at <UTC_TIMESTAMP> `
  --provider openai --model <MODEL_ID> `
  --shadow --allow-model-call --commit) | ConvertFrom-Json

$Checkpoint = $DraftLesson.checkpoint.content_hash
$LessonDraft = $DraftLesson.result.mistake_lesson_proposal_draft
```

`[WRITE] [HUMAN DECISION]` Records only a proposed lesson:

```powershell
$Lesson = (& $Python @Base record-lesson $Workflow `
  --checkpoint-hash $Checkpoint `
  --draft-id $LessonDraft.draft_id `
  --draft-hash $LessonDraft.content_hash `
  --confirm-draft-hash $LessonDraft.content_hash `
  --human-actor "human:<REVIEWER>" `
  --recorded-at <UTC_TIMESTAMP> `
  --commit) | ConvertFrom-Json

$Checkpoint = $Lesson.checkpoint.content_hash
```

`[WRITE] [HUMAN DECISION]` Separately validates thresholds and activates:

```powershell
$Activated = (& $Python @Base activate-lesson $Workflow `
  --checkpoint-hash $Checkpoint `
  --lesson-id $Lesson.lesson.lesson_id `
  --lesson-hash $Lesson.lesson.content_hash `
  --confirm-lesson-id $Lesson.lesson.lesson_id `
  --scope-type exact_case `
  --human-actor "human:<REVIEWER>" `
  --reason "Human verified the evidence and selected scope." `
  --recorded-at <UTC_TIMESTAMP> `
  --commit) | ConvertFrom-Json
```

Use `scoped` or `general` only when existing Phase 11C evidence thresholds are
met. The command never overrides those thresholds.

## Status and Verification

Both commands are `[READ ONLY] [NO MODEL]`:

```powershell
& $Python @Base status $Workflow
& $Python @Base verify $Workflow
```

Checkpoints are stored below
`<database-directory>\phase11-shadow-workflows`. Exact committed retries are
idempotent. Changed inputs, stale checkpoint hashes, branches, path traversal,
and tampered files fail closed.
