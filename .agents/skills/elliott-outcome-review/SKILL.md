---
name: elliott-outcome-review
description: Use when Codex creates or freezes Phase 11 forecasts, captures post-cutoff observations, evaluates forecast outcomes, records human outcome reviews, proposes or retrieves mistake-memory lessons, replays historical forecasts, modifies or tests Phase 11 systems, or integrates future technical agents with Phase 11. Do not use for ordinary Elliott chart analysis that does not touch forecast, outcome-evaluation, replay, or mistake-memory records.
---

# Elliott Outcome Review

Follow `docs/phase11_forecast_outcome_memory_spec.md` as authoritative. Inspect
the relevant contracts in `elliott_ai/forecast_records.py`,
`elliott_ai/forecast_outcomes.py`, `elliott_ai/mistake_memory.py`, persistence
in `elliott_ai/knowledge.py`, and the Phase 11 tests before changing behavior.

## Workflow

1. Verify the exact UTC decision-time cutoff, every dataset cutoff and hash,
   feed identity, timeframe, price basis, completed-candle state, and
   provisional status.
2. Freeze the forecast before loading future candles. Preserve main and
   alternative hypotheses as separate immutable records.
3. Accept only immutable observations strictly after the forecast cutoff and
   enforce feed, session, adjustment, price-basis, completeness, and horizon
   compatibility.
4. Run deterministic outcome evaluation before human interpretation. Preserve
   separate claim, price, timing, main-count, and alternative-count results.
5. Record human review as a new immutable review. Correct deterministic scoring
   only with a superseding evaluation; never rewrite the reviewed result.
6. Propose lessons only from eligible reviewed outcomes. Require an explicit
   human event before activation and retain supporting cases, counterexamples,
   rejected proposals, and supersession history.
7. Generate independent candidate counts before retrieving memory. Retrieve
   only lessons active and fully available by the new analysis cutoff; blind
   mode returns none. Treat retrieved lessons as audit warnings only.
8. Use append-only create/get/list persistence, canonical hashes, restricted
   foreign keys, and linear parent or supersession chains.
9. Run `scripts.test_forecast_records`, `scripts.test_forecast_outcomes`, and
   `scripts.test_mistake_memory` with the bundled Python runtime. For database
   work, also run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` and
   preserve the required pre-migration backup and audit.

## Guardrails

- Never reconstruct a legacy forecast from later knowledge or hindsight.
- Never expose future observations, reviews, lessons, or events across a
  historical cutoff.
- Never change hard Elliott rules, model weights, prompts, probabilities,
  confidence, or indicators automatically.
- Never activate, merge, generalize, or apply a lesson automatically.
- Never let human review rewrite deterministic candle or claim outcomes.
- Never let company knowledge, fundamentals, news, or scenarios control the
  frozen technical premise.
- Never use outcome valence to change Phase 5 analogue eligibility, ranking,
  ordering, or hashes.
- Never bypass append-only persistence, canonical hashing, provenance,
  restricted deletion, or explicit supersession.
- Never activate Phase 11 in the live analysis pipeline without explicit user
  approval for that integration phase.

Stop and report when required cutoffs, immutable references, hashes, or human
approval are missing. Do not invent or repair them from current knowledge.
