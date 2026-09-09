# Repository Instructions

These rules apply to all work in this repository unless the user explicitly
requests a scoped exception.

## Preserve Technical-Analysis Independence

- Keep the existing Elliott Wave pipeline price-first and technically
  independent.
- Do not modify Elliott Wave calculations, degree resolution, deterministic
  validation, indicators, pivots, metrics, confidence, readiness, targets, or
  invalidations unless explicitly requested.
- Fundamentals, news, macro data, sentiment, and event information must never
  influence the initial technical count or degree-resolution process.

## Freeze the Technical Result

- Treat the completed, stored output of `ElliottAgent.resolve_degrees()` and
  its deterministic validation as immutable input to downstream reasoning.
- A later module may interpret, support, contradict, or stress-test the frozen
  technical scenario, but must not silently relabel, overwrite, or recalculate
  it.
- Downstream results must retain an explicit reference to the frozen technical
  result, its cutoff, and its source hashes.

## Post-Technical Market Scenario Engine

- Integrate the future Market Scenario Engine after `resolve_degrees()` has
  completed and stored the technical result, and before the final report is
  presented.
- Make the engine part of the normal analysis pipeline. A standalone debugging
  or rerun command may exist, but it must call the same engine.
- Given a frozen technical projection, assess whether the complete real-world
  environment is capable of producing the projected move.
- Examine company-specific, sector, competitor, macroeconomic, valuation,
  liquidity, positioning, sentiment, and scheduled-event factors.
- Model interacting forces; do not reduce a scenario to one catalyst when
  several drivers may be involved.

## Competing Narratives and Timelines

- Generate multiple plausible, multi-factor narratives.
- Every primary narrative must include at least two independent drivers.
- Include a scenario where the projected move occurs without major
  company-specific bad news, such as through valuation compression, liquidity,
  positioning, sentiment, or sector rotation.
- Compare the projected technical completion window with all material known
  events.
- Explain how the interpretation changes if completion occurs before, around,
  or after those events.

## Evidence and Provenance

- Distinguish confirmed facts, scheduled events, model interpretations,
  hypothetical catalysts, and unknown or unavailable information.
- For current factual evidence, retain the source, publication date, retrieval
  timestamp, provider/feed identity, and applicable cutoff.
- Never invent missing evidence, unpublished facts, or future events.
- Fail transparently when current research is unavailable, stale, incompatible,
  or outside the permitted cutoff.

## Adversarial Review

- Run a separate adversarial review after generating narratives.
- Attempt to disprove the primary narrative by checking for cherry-picking,
  omitted opposing evidence, unsupported causality, stale evidence, timeline
  mismatches, excessive assumptions, and simpler explanations.
- State the strongest argument against the primary scenario.
- State technical invalidation signals separately from fundamental
  contradiction signals.

## Language and Certainty

- Never claim that an Elliott Wave count predicts a specific news event.
- Use conditional wording such as `could`, `may`, `would be consistent with`,
  and `one plausible combination is`.
- Do not use unsupported causal claims such as `this will happen because`, `the
  chart proves`, or `the company must announce`.

## Engineering Expectations

- Use the repository skill `$elliott-outcome-review` for all Phase 11,
  forecast-outcome, historical-replay, mistake-memory, or forecast-learning
  work.
- Use `$elliott-adaptive-recount` when operating or modifying the staged
  adaptive blind recount and its pre-freeze recursive proof workflow.
- Prefer typed schemas, explicit contracts, deterministic validation, immutable
  references, and content hashes.
- Preserve source traceability, retrieval cutoffs, and reproducible outputs.
- Keep technical evidence and post-technical scenario evidence logically and
  visibly separate.
- Add focused tests for every new behavior, including cutoff isolation,
  immutability, unavailable research, competing narratives, and adversarial
  review.
- Do not implement the Market Scenario Engine unless explicitly requested.
- Keep this file concise. Put the detailed Market Scenario Engine workflow,
  schemas, and source policies in a repository-local Skill or dedicated
  specification file.
