# Phase 4 Historical Case Analyst Completion Report

**Completed:** 30 July 2026  
**Scope:** Historical causal reasoning over a frozen, validated evidence packet

## Implementation Summary

Phase 4 adds `HistoricalCaseAnalyst`, the first LLM reasoning service in the
historical Market Scenario work. It accepts one finalized
`HistoricalEvidencePacket`, performs bounded causal synthesis, optionally runs
an adversarial second pass, validates all model output deterministically, and
returns a typed, content-hashed `HistoricalAnalysisExecutionResult`.

It remains separate from Elliott analysis, the active agent pipeline, current
market scenarios, CLI, reporting, persistence, retrieval, and web research.

## Files

Created:

- `elliott_ai/historical_analyst.py`
- `scripts/test_historical_analyst.py`
- `HISTORICAL_CASE_ANALYST_PHASE4_COMPLETION_REPORT.md`

Modified:

- `elliott_ai/historical_market.py`
- `elliott_ai/providers.py`
- `scripts/test_historical_market.py`
- `docs/market_scenario_engine_spec.md`

No database, CLI, reporting, Elliott, technical prompt, or active-agent file
was modified.

## Input and Output Boundary

Input is accepted only when:

- it is a `HistoricalEvidencePacket`;
- fresh deterministic validation is valid;
- stored and fresh validation results match;
- nested hashes, provenance, references, and cutoff rules remain valid; and
- the packet is therefore finalized rather than merely assembled.

The service snapshots the packet and its content hash. Providers receive only
a detached JSON copy. A final immutability assertion confirms that the source
packet and hash did not change.

Output uses:

- execution schema `historical-case-analyst-1.0.0`;
- adversarial schema `historical-adversarial-review-1.0.0`;
- causal schema `historical-causal-analysis-1.1.0`; and
- analyst validation version
  `historical-case-analyst-validation-1.0.0`.

The execution result records success, analysis, deterministic validation,
provider, model, prompt version, attempts, adversarial state and
recommendation, errors, warnings, input/output hashes, generation time, full
adversarial evidence, and its own content hash.

## Prompt Versions

- Causal synthesis: `historical-causal-prompt-1.0.0`
- Adversarial review: `historical-causal-adversarial-prompt-1.0.0`

Both prompts freeze packet facts and IDs, separate temporal evidence lanes,
forbid invention and unsupported certainty, require conditional causal
language, embed their JSON schema, and prohibit surrounding prose.

The combined system and user prompt is bounded on initial, correction, and
adversarial calls. Retry feedback contains only deterministic rule, path, and
message information.

## Provider Integration

The existing `AnalysisProvider` contract remains the base interface.
`HistoricalCaseAnalyst` uses additive `generate_strict_json()` support when
available and otherwise calls the existing `generate()` method.

- OpenAI uses the existing Responses API adapter plus strict JSON parsing.
- Ollama uses its native schema `format`, temperature `0`, seed `0`, and strict
  JSON parsing.
- Packet mode returns a structured no-reasoning unavailable state.
- Offline providers named `fixture` support deterministic tests.
- Unsupported providers and infrastructure failures return structured
  failures without retry.

The pre-existing lenient provider methods remain unchanged, preserving the
active Elliott path. The strict parser rejects Markdown fences, prefixes,
suffixes, extra prose, non-objects, and ambiguous JSON.

## Evidence and Temporal Enforcement

The additive causal 1.1 contract records:

- supporting evidence IDs;
- initiating-condition evidence IDs;
- retrospective explanations; and
- retrospective evidence IDs.

Deterministic validation requires:

- every referenced evidence and event ID to exist;
- current-schema causal claims to have supporting evidence;
- initiating conditions to use only `known_before_move` evidence;
- post-move support to be explicitly labeled retrospective;
- unavailable evidence not to act as support or contradiction;
- triggering events not to be unavailable, retrospective, or after the move;
- contradicting evidence to remain present;
- transmission mechanisms to use the controlled enum;
- multi-driver support when `no_single_catalyst` is true; and
- model provenance and cutoff values to be application-owned.

Unsupported phrases include `definitely caused`, `proves that`, `guaranteed`,
`certainly happened because`, `the chart predicted`, and `must have caused`.

Legacy `historical-causal-analysis-1.0.0` records remain readable and valid.
They receive an explicit warning that typed Phase 4 linkage was unavailable.

## Adversarial Review

The second pass reviews the first pass against the same packet for:

- hindsight bias and post-outcome leakage;
- cherry-picking and unsupported causal links;
- omitted bullish and bearish evidence;
- excessive single-catalyst reliance;
- market regime, sector, and positioning omissions;
- simpler explanations;
- contradictory timing; and
- magnitude mismatch.

It returns `keep`, `revise`, `reject`, or `insufficient_evidence`.
`revise` requires a complete replacement causal analysis that passes the same
validator. Other recommendations prohibit a replacement. Review references
must exist in the packet, all required assessments must be non-empty, and at
least one simpler-explanation assessment is required.

## Retry Behavior

Default attempts are two per pass, configurable from one to three.

Retried:

- malformed or non-object output;
- missing, unknown, or mistyped fields;
- invalid enums;
- dangling references;
- temporal leakage;
- unsupported certainty;
- missing support or contradiction;
- invalid replacement analysis; and
- other model-correctable deterministic validation failures.

Not retried:

- invalid or unfinalized packets;
- unsupported or packet-only providers;
- credential, HTTP, or network infrastructure failures;
- prompt-size failures; and
- genuine evidence insufficiency with explicit missing evidence.

Retry exhaustion produces a structured failure instead of throwing.

## Offline Test Strategy

`FixtureProvider` is a deterministic queue-backed provider. It stores detached
prompt, schema, and packet copies and returns predefined objects or errors. It
also exercises the strict provider method. Tests cover valid causal outputs,
both move directions, every adversarial recommendation, correction retries,
provenance override, hashes, serialization, prompt bounds, immutability,
strict extra-prose rejection, and repeated-run determinism.

No test used live OpenAI, Ollama, credentials, web access, or current data.

## Verification

Focused command:

```powershell
python -m unittest scripts.test_historical_market scripts.test_historical_analyst
```

Result:

```text
Ran 74 tests in 0.689s
OK
```

Complete repository command using the Codex workspace runtime:

```powershell
& 'C:\Users\Parwa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s scripts -p "test_*.py"
```

Result:

```text
Ran 520 tests in 38.683s
OK
```

The system Python was also tried first. It ran 509 tests but could not import
`scripts/test_elliott_analysis_layers.py` because that interpreter did not
contain `pandas`. This was an environment dependency issue; the complete
workspace-runtime run above includes that test module and passes.

## Compatibility and Limitations

Compatibility risk is limited to the causal schema version moving from 1.0 to
1.1. The new fields have defaults, the old version remains accepted, and a
focused test proves old JSON without those fields remains readable.

OpenAI's historical strict path uses strict JSON parsing and deterministic
post-processing but does not impose one cross-model temperature parameter,
because the shared adapter supports models with different generation controls.
Ollama uses deterministic local options.

Deferred:

- historical evidence research and extraction;
- live web, news, filings, events, fundamentals, and market-data access;
- database persistence;
- CLI and reporting;
- similarity retrieval and embeddings;
- current-market scenario generation;
- active Market Scenario pipeline integration;
- active Elliott integration;
- prediction, probability, trade decisions, and automatic wave resolution; and
- semantic per-sentence claim-to-evidence graphs beyond the typed evidence-ID
  collections in the approved causal contract.
