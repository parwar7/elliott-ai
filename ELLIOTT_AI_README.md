# Elliott AI MVP

This project turns the existing Elliott Wave database into an evidence-bound AI agent. It does **not** train a foundation model from zero. The current brain becomes the retrieval layer, the existing Python rules remain the calculation layer, and an OpenAI or local Ollama model performs the comparison and explanation layer.

## What Is Implemented

1. `AI_BRAIN_MASTER_RULES.json` and `AI_BRAIN_CURRENT.md` are indexed as canonical knowledge.
2. Analysis and recount files are indexed as case reports; raw OHLCV and browser dumps are excluded.
3. SQLite FTS5 retrieves only the relevant rule and case sections for each question.
4. TradingView JSON exports are parsed without third-party packages. Normalized volume, EWO, MACD, optional RSI, ATR, Bollinger/Keltner position, arithmetic/log scale fit, date range, data quality, and price-pivot candidates are calculated deterministically.
5. Existing impulse and correction verification functions can be applied to supplied wave rows.
6. Every model answer must cite supplied evidence IDs and use the required JSON output contract.
7. Every run is saved. Corrections are versioned separately, and a run becomes reusable memory only after a final degree resolution and explicit user approval.
8. Providers: evidence-packet mode, OpenAI Responses API, and local Ollama.
9. After exact wave boundaries are resolved, Python calculates per-wave indicators, optional aligned RSI evidence, arithmetic and logarithmic Fibonacci ratios, elapsed duration, soft degree-duration anomaly flags, Wave 2/4 alternation, and Elliott channels.
10. Optional benchmark, breadth, yield, options, fundamentals, news, and order-book JSON adapters are available. They never invent unavailable values.
11. Every measurable candidate wave receives a cutoff-safe, versioned fingerprint with deterministic price, structure, volume, EWO, MACD, volatility, and optional RSI features. Structurally paired fingerprints can be compared without assigning a label or probability.
12. Reviewed experience endpoints can be structurally filtered and compared across 56 versioned dimensions with explicit differences, missing states, provenance, and no outcome-based ranking.
13. Human-reviewed historical outcomes can be attached only after Phase 5A.4 retrieval has validated and frozen its selected cases, order, tiers, limit, and hash. Outcome evidence is contextual and cannot feed back into retrieval.
14. Historical endpoint cases can now be populated through a versioned, append-only human review workflow. Discovery and technical validation never accept a case; structural review, outcome review, and final acceptance are separate content-hashed events.

## First Run On Windows 11

Open PowerShell in this workspace and build the index:

```powershell
python -m elliott_ai index
python -m elliott_ai stats
python -m elliott_ai search "top-down Wave 3 volume EWO rules"
```

Test the complete data and retrieval path without making a paid model call:

```powershell
python -m elliott_ai analyze "Strict blind recount of GOOGL from highest degree downward" --symbol NASDAQ:GOOGL --blind --provider packet --output googl_blind_packet.json
```

`--blind` excludes previous counts for the requested symbol and retrieves rules only. The agent automatically chooses the TradingView export with the most bars for each available timeframe. Explicit files can be supplied with repeated `--ohlcv` options.

Each selected timeframe now supplies three compact reasoning views:

1. A continuous 120-bar full-history OHLCV/EWO map for top-down parent structure.
2. The latest 120 unchanged native bars for exact current-wave analysis.
3. Three native pivot ladders with endpoint EWO and volume at different sensitivities.

The full-history map uses consecutive OHLCV aggregation, so its bucket boundaries are not automatically orthodox Elliott pivots. The native bars and pivot ladders must confirm exact labels. This design follows the 100-140 bar zoom rule without sending every stored candle or using prior symbol counts.

By default the views include EWO, MACD, normalized volume, volatility bands, and scale diagnostics. To test one layer in isolation, repeat only the wanted feature flags:

```powershell
python -m elliott_ai analyze "Blind MACD-only recount of NFLX" --symbol NASDAQ:NFLX --blind --feature macd --provider packet
```

Supported values are `ewo`, `macd`, `volume`, `volatility`, `scale`, and `rsi`. Omitting `--feature` enables the default set, which deliberately excludes RSI.

RSI is disabled by default. Add it to the normal defaults with either explicit configuration:

```powershell
python -m elliott_ai analyze "Blind recount with optional RSI" --symbol NASDAQ:NFLX --blind --enable-rsi --provider packet
$env:ELLIOTT_ENABLE_RSI = "true"
```

Use `--feature rsi` only when intentionally testing RSI in isolation. The canonical calculation is Wilder RSI(14): the first value uses the arithmetic mean of gains and losses across the first 14 consecutive close-to-close changes. Every later average is `((previous average * 13) + current gain or loss) / 14`, and RSI is `100 - 100 / (1 + average_gain / average_loss)`. An all-gain window returns 100, an all-loss window returns 0, and a fully flat gain/loss state returns 50. Missing or non-finite closes reset initialization; the implementation never bridges a data gap.

RSI is evidence only. It is compared only at explicitly aligned price pivots from the same feed and timeframe. Unaligned or incompatible comparisons are reported as unavailable or incomparable and never reduce readiness or invalidate an Elliott count.

## Versioned Wave Fingerprints

Phase 3 fingerprints use feature schema `wave-fingerprint-1.0.0`, calculation version `wave-fingerprint-calc-1.0.0`, role-template version `wave-role-template-1.0.0`, and unified evidence contract `1.0.0`.

The fingerprint layer runs only after the structural analysis supplies a candidate wave. It records observed price behavior, child structure, duration, channel fit, acceleration, normalized volume, effort versus result, EWO, MACD, ATR/volatility, and optional RSI. A fingerprint cannot assign or change an Elliott label.

Every calculation filters the source candle set at `data_cutoff` before calculating indicators. A later cutoff creates a different immutable content hash. Appending or changing candles after the original cutoff cannot alter the earlier fingerprint.

Structurally paired siblings are compared automatically where available: B/A, C/A, 2/1, 3/1, 4/3, 5/3, X/W, Y/W, X2/Y, and Z/Y. Missing references remain unavailable rather than becoming zero. Volume is marked incomparable across different venues, market types, scopes, or feed identities.

SQLite stores immutable records in `wave_observations`, `wave_fingerprints`, and `feature_schema_versions`. Existing runs are not rewritten. Fingerprints are deduplicated by canonical SHA-256 content hash, and older schema payloads remain readable as raw versioned JSON.

## Reviewed Experience Cases (Phase 5A.1)

Phase 5A.1 can turn a current, explicitly reviewed Phase 4 outcome into a pending experience candidate. It stores endpoint DNA from the original Phase 3 fingerprint, confirmation DNA from timed Phase 4 snapshots and transitions, and resolved-outcome DNA from the human-reviewed outcome. It does not recalculate indicators, perform similarity retrieval, train a model, or accept its own output.

List eligible sources and create a pending version:

```powershell
python -m elliott_ai experience candidates
python -m elliott_ai experience create CORRECTION_CASE_ID
python -m elliott_ai experience inspect EXPERIENCE_CASE_ID
python -m elliott_ai experience dna EXPERIENCE_CASE_ID --kind endpoint
```

Acceptance requires a named reviewer and the explicit human-confirmation flag:

```powershell
python -m elliott_ai experience review EXPERIENCE_CASE_ID --action accept --reviewer "Parwa" --rationale "Reviewed against the source chart and outcome lineage" --human-confirmed
```

Rejecting, quarantining, revising, quality assignment, and tag removal all append history; they never overwrite prior records:

```powershell
python -m elliott_ai experience review EXPERIENCE_CASE_ID --action quarantine --reviewer "Parwa" --rationale "Feed provenance requires review"
python -m elliott_ai experience revise-review EXPERIENCE_CASE_ID --parent-review REVIEW_ID --action accept --reviewer "Parwa" --rationale "The mismatch was resolved" --human-confirmed
python -m elliott_ai experience quality EXPERIENCE_CASE_ID --status medium --reviewer "Parwa" --rationale "Core anchors are complete; optional context is partial"
python -m elliott_ai experience tag EXPERIENCE_CASE_ID --tag canonical:expanded_flat --action add --actor "Parwa"
python -m elliott_ai experience tag EXPERIENCE_CASE_ID --tag canonical:expanded_flat --action remove --actor "Parwa" --rationale "Reclassified"
python -m elliott_ai experience export --output reviewed_experience.json --accepted-only
```

A revised Phase 4 outcome creates a new experience-case version under the same `market_episode_id`. The prior version remains stored but is excluded from the active accepted pool. No existing run, fingerprint, correction case, or outcome is converted automatically.

## Structural Experience Filtering (Phase 5A.2)

Phase 5A.2 filters the human-accepted experience pool before any similarity calculation. It compares endpoint DNA only, applies an exhaustive versioned role and family matrix, and returns eligible and excluded cases in stable identity order. It does not calculate a similarity score, rank cases, read confirmation DNA, or read outcome DNA.

Inspect the matrix or filter from one current experience case:

```powershell
python -m elliott_ai experience matrix
python -m elliott_ai experience filter EXPERIENCE_CASE_ID
```

The conservative default permits exact or compatible structures at the same or adjacent Elliott degree. Context-family, far-degree, and cross-market comparisons require explicit opt-in:

```powershell
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --same-degree-only
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --no-adjacent-degree
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --allow-far-degree
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --allow-context-family
python -m elliott_ai experience filter EXPERIENCE_CASE_ID --allow-cross-market
```

Each result states its comparison level, structural class, matrix rule and location, missing endpoint groups, incomparable groups, and non-scored metadata differences. Self matches, another version of the current episode, inactive versions, unreviewed or quarantined cases, invalid hashes, cutoff leakage, unsupported schemas, missing mandatory DNA or provenance, and structurally incompatible roles are excluded explicitly. Feed differences do not reject otherwise valid structure; raw volume, volume profile, funding, and basis become incomparable instead.

## Deterministic Experience Comparison (Phase 5A.3)

Phase 5A.3 compares the current endpoint DNA with only the cases that survive Phase 5A.2. Its versioned specification contains 56 identity, geometry, momentum, volume, volatility, multi-timeframe, market-context, and cutoff-confirmation dimensions. Every dimension declares its source path, data type, method, normalization, tolerance, permitted progressive levels, and missing/provenance behavior.

Inspect the complete specification:

```powershell
python -m elliott_ai experience comparison-spec
```

Produce JSON or a human-readable comparison:

```powershell
python -m elliott_ai experience compare CURRENT_EXPERIENCE_CASE_ID --format json --output analogue_comparison.json
python -m elliott_ai experience compare CURRENT_EXPERIENCE_CASE_ID --format text --explain --show-provenance --show-hashes --output analogue_comparison.md
```

Optional controls include repeated `--candidate CASE_ID`, `--level 1|2|3|4` as the maximum progressive level, `--spec-version`, and the same degree, context-family, far-degree, and cross-market switches used by the structural filter.

The methods are descriptive pair, signed/absolute numeric difference, ratio difference, tolerance-band comparison, categorical exact match, matrix-backed categorical compatibility, and ordered-state distance. Tolerance boundaries are inclusive. Missing values return `missing_current`, `missing_historical`, or `missing_both`; invalid values return `unsupported_by_spec`; feed and provenance conflicts remain explicitly incomparable. Nothing is imputed as zero.

Raw EWO, MACD, RSI, candle-duration, and band-pierce fields are restricted by their level, feed, and timeframe contracts. Normalized volume ratios may remain comparable across different feeds only when volume scope agrees. Every result preserves source hashes, endpoint cutoffs, immutable fingerprint/snapshot reference checks, and Phase 5A.2 filter lineage.

The output order is stable identity order, not relevance order. Phase 5A.3 creates no grouped score, overall distance, best analogue, probability, prediction, wave selection, trade decision, database match record, or outcome-based feature. Confirmation DNA and reviewed outcome DNA are never read by the comparator.

## Deterministic Analogue Retrieval (Phase 5A.4)

Phase 5A.4 presents Phase 5A.3 comparisons to a human reviewer in a reproducible order. Policy `endpoint-evidence-lexicographic` version `1.0.0` partitions structurally eligible cases into three explicit tiers and then applies declared lexicographic keys. It creates no blended similarity score, grouped numerical distance, prediction, wave decision, or trade instruction.

Inspect the complete policy or retrieve analogues:

```powershell
python -m elliott_ai experience retrieval-policy --format text
python -m elliott_ai experience retrieve CURRENT_EXPERIENCE_CASE_ID --format json --output analogue_retrieval.json
python -m elliott_ai experience retrieve CURRENT_EXPERIENCE_CASE_ID --format text --explain --show-ordering-keys --show-provenance --show-hashes --include-comparison-details --output analogue_retrieval.md
```

The three tiers are:

1. Tier 1: Level 1 exact structural compatibility, all mandatory structural dimensions comparable, and no feed/provenance incomparability.
2. Tier 2: Level 1-3 exact or compatible structure with all mandatory structural dimensions comparable, but not every Tier 1 condition.
3. Tier 3: a Phase 5A.2-eligible broader, context-only, cross-market, or partially available analogue.

The policy orders tier and comparison level first. It then penalizes mandatory incomparability, missing data, and omission before rewarding availability. The same transparent state, tolerance, and categorical-match counts are applied in versioned group order, followed by aggregate counts and stable experience-case ID as the final tie-breaker. Raw Phase 5A.3 dimensions remain attached to every returned analogue.

Optional controls include repeated `--candidate CASE_ID`, `--comparison-level 1|2|3|4`, `--limit`, `--policy`, `--policy-version`, `--spec-version`, and the Phase 5A.2 filter switches. `--level` remains an alias for `--comparison-level` on this command.

Missing, feed-incomparable, provenance-incomparable, structurally inapplicable, and unsupported values remain explicit and can never improve ordering. Cutoff proximity and diversity controls are not used. Retrieval persists nothing and adds no database tables. The empty accepted experience pool is valid and returns `unavailable_empty_accepted_experience_pool` without fabricating a case.

Reviewed outcomes, confirmation DNA, and post-cutoff evidence are not loaded by retrieval and cannot affect eligibility, comparison evidence, tier, order, selection, tie-breaking, or the result hash. Outcome presentation remains outside Phase 5A.4.

## Historical Reviewed Outcome Evidence (Phase 5B)

Phase 5B answers what happened after each independently retrieved historical endpoint. It first runs and freezes the unchanged Phase 5A.4 result, then loads the reviewed outcome linked to each already-selected case in preserved order. It does not forecast the current endpoint, vote across cases, calculate an outcome frequency, choose a wave count, or produce a trade instruction.

Inspect the versioned evidence specification, taxonomy, and observation horizon:

```powershell
python -m elliott_ai experience outcome-spec --format text
```

Audit one historical experience outcome without adding it to a retrieval result:

```powershell
python -m elliott_ai experience outcomes EXPERIENCE_CASE_ID --format text --explain --show-provenance --show-hashes --include-outcome-details
```

Retrieve first and then attach reviewed historical outcomes:

```powershell
python -m elliott_ai experience evidence CURRENT_EXPERIENCE_CASE_ID --format json --output historical_outcome_evidence.json
python -m elliott_ai experience evidence CURRENT_EXPERIENCE_CASE_ID --format text --explain --show-provenance --show-hashes --include-retrieval-details --include-outcome-details --output historical_outcome_evidence.md
```

The default horizon is `reviewed-resolution-window` version `1.0.0`. It begins at the immutable endpoint-DNA cutoff and ends at the accepted outcome resolution cutoff. The maximum available observation timestamp comes from confirmation DNA. If that lineage ends before resolution, the record is explicitly incomplete and right-censored.

Only an active human-accepted experience with a current accepted outcome review can produce accepted evidence. Missing, unreviewed, partially reviewed, rejected, stale, hash-failing, or mismatched outcomes remain visible with explicit states. Symbol, timeframe, degree, endpoint role, feed, provider, fingerprint, snapshot, and cutoff linkage are checked before presentation.

The stored outcome lineage supports reviewed structural category, interpretation, alternatives, explicit confirmation events, and elapsed UTC duration. It does not contain a complete post-endpoint candle series or terminal price, so MFE, MAE, movement, retracement, extension, drawdown, post-endpoint RSI, and post-endpoint volume are reported as unavailable rather than inferred. Descriptive aggregation is not implemented.

Phase 5B creates no tables and persists no evidence result. Its combined hash includes the frozen Phase 5A.4 hash, ordered selected cases, accepted or unavailable evidence records, specification and horizon versions, and presentation options. The original Phase 5A.4 retrieval hash is retained unchanged.

## Historical Experience Population And Review

The population workflow uses specification `historical-experience-workflow-1.0.0`. It discovers immutable Phase 3 fingerprints paired with root Phase 4 endpoint snapshots, assembles a draft, validates hashes, provenance, feed, symbol, timeframe, degree, endpoint, role, family, and cutoffs, and then waits for explicit human reviews.

Inspect the contract and discover candidates:

```powershell
python -m elliott_ai experience workflow-spec --format text
python -m elliott_ai experience workflow-candidates --format text --explain --show-provenance --show-hashes
```

Create and validate one draft:

```powershell
python -m elliott_ai experience draft CORRECTION_CASE_ID --format json
python -m elliott_ai experience validate WORKFLOW_CASE_ID --format text --explain
python -m elliott_ai experience workflow-inspect WORKFLOW_CASE_ID --format json
```

Structural and outcome reviews are JSON objects supplied non-interactively. Each review has a named reviewer, explicit decision, review version, review hash, event hash, and immutable parent event. An accepted structural review must explicitly confirm role, degree, endpoint, and pattern family. RSI, volume, EWO, MACD, Fibonacci, and channel notes remain evidence and cannot create a hard invalidation by themselves.

```powershell
python -m elliott_ai experience review-structure WORKFLOW_CASE_ID --decision accepted --review-file structural_review.json --reviewer "Parwa"
python -m elliott_ai experience review-outcome WORKFLOW_CASE_ID --decision accepted --review-file outcome_review.json --reviewer "Parwa"
python -m elliott_ai experience acceptance-status WORKFLOW_CASE_ID --format text --explain
python -m elliott_ai experience accept WORKFLOW_CASE_ID --reviewer "Parwa" --notes "Final explicit human acceptance"
```

Intermediate review states are explicit: `structural_review_accepted`, `structural_review_rejected`, `structural_review_needs_revision`, `structural_review_ambiguous`, and their outcome-review equivalents. Only the final `accepted` state enters the existing Phase 5A experience pool.

Conflicting reviews produce an unresolved disagreement and block final acceptance. Resolution requires a later human review whose JSON includes `resolves_event_ids` for every conflicting event. There is no majority vote, senior-review role, or automatic adjudication.

List final workflow state or reject, withdraw, and supersede explicitly:

```powershell
python -m elliott_ai experience pool --state all --format text
python -m elliott_ai experience reject WORKFLOW_CASE_ID --reviewer "Parwa" --reason source_not_suitable --notes "Reason"
python -m elliott_ai experience withdraw WORKFLOW_CASE_ID --reviewer "Parwa" --notes "Reason"
python -m elliott_ai experience supersede OLD_WORKFLOW_CASE_ID --replacement NEW_WORKFLOW_CASE_ID --actor "Parwa"
```

Final acceptance runs in one SQLite `BEGIN IMMEDIATE` transaction. It revalidates every prerequisite, creates or verifies the existing Phase 5A.1 experience case and three Pattern DNA rows, creates the existing human acceptance review, and appends the linked workflow acceptance event. Any failure rolls all of those changes back.

The workflow adds only `experience_workflow_cases` and `experience_workflow_events`. It does not store post-endpoint candles or calculate post-endpoint indicators. It adds no machine learning, probability, prediction, wave resolution, analogue voting, trade decision, or outcome-aware ranking.

Read `evidence_manifest` before paying for a model call. Continue when `cross_timeframe_reconciliation` is `passed` or `passed_with_volume_limitations`. The latter allows lower-timeframe price structure but prohibits mixing daily and intraday volume. A `failed` result quarantines lower-timeframe price boundaries.

For a clean TradingView export, use the exact same:

1. Exchange-qualified symbol, such as `NASDAQ:GOOGL`.
2. Regular or extended session setting on every timeframe.
3. Split/dividend adjustment setting.
4. Chart timezone and data feed.
5. Complete session coverage, including every intraday bar needed to reconstruct each daily candle.

Re-run the free packet command after replacing the mismatched files. Do not make the paid call while reconciliation is failed.

The local TradingView bridge can capture candles directly from the open chart while recording the actual provider substitution and chart settings:

```powershell
node scripts\tv_cdp.mjs candles tv_googl_daily_verified.json NASDAQ:GOOGL 1D regular 6000
node scripts\tv_cdp.mjs candles tv_googl_1h_verified.json NASDAQ:GOOGL 60 regular 20000
```

TradingView may resolve `NASDAQ:GOOGL` to `BATS:GOOGL / Cboe One` when official NASDAQ intraday data is unavailable on the account. In that case, price structure can be reconciled statistically, but Cboe intraday volume must never be compared directly with consolidated daily volume. Disputed dates remain feed-specific pivots.

## Use OpenAI

Set the key only in the current PowerShell session:

```powershell
$env:OPENAI_API_KEY = "your-key"
$env:ELLIOTT_MODEL = "gpt-5.6-terra"
python -m elliott_ai analyze "Perform a strict blind GOOGL recount from the highest supported degree to the lowest evidence-supported degree. Validate price rules and child subdivisions before Fibonacci, volume, and EWO. Test alternate counts and nested 1-2 structures." --symbol NASDAQ:GOOGL --blind --provider openai --output googl_agent_result.json
```

`gpt-5.6-terra` is the initial balanced-cost default. The model can be overridden with `--model`. The provider uses the Responses API through the Python standard library, so no OpenAI SDK installation is required.

## Use A Local Model

After Ollama is installed, running, and a model has been downloaded:

```powershell
python -m elliott_ai analyze "Audit the current GOOGL count" --symbol NASDAQ:GOOGL --provider ollama --model YOUR_LOCAL_MODEL
```

A small local model is useful for development, but it should not be trusted to assign complex multidegree structures until it passes the evaluation set.

## Add Books And Videos Safely

Convert a book chapter or video transcript to `.txt`, `.md`, or structured `.json`, then register it as candidate research:

```powershell
python -m elliott_ai add-source path\to\transcript.txt --kind video_transcript --status candidate
```

Candidate research cannot silently override the master rules. After an analysis has been manually checked and its strict degree resolution passes the final-readiness gate, promote that run to approved case memory:

```powershell
python -m elliott_ai runs
python -m elliott_ai accept 1 --title "Reviewed RKLB canonical recount" --note "Price anchors and lower-timeframe subdivisions manually confirmed"
```

`accept` snapshots the corrected degree resolution, not the original model answer. Packet-only, provisional, invalid, low-confidence, and internally unproven counts are rejected. This approval gate is how the agent becomes more consistent without storing its own mistakes as truth.

## Recursively Refine A Provisional Run

A first blind pass may establish parent candidates while marking their children unproven. Do not repeat the same `analyze` command. Build focused 100-140 bar windows from that run instead:

```powershell
python -m elliott_ai refine 14 --provider packet --output googl_refinement_packet.json
```

The free packet must report zero unavailable zoom windows. It automatically adds a full-history control, any omitted origin or gap intervals, every proposed completed parent wave, and the active segment. Each window selects the complete source timeframe closest to 120 bars and aggregates consecutive bars only when necessary.

Then perform the paid refinement pass:

```powershell
python -m elliott_ai refine 14 --provider openai --output googl_blind_analysis_refined.json
```

The parent run is supplied only as an untrusted hypothesis. The refinement model must recount each `Z` window and may reject, split, merge, or relabel it. The refined result still requires deterministic wave-boundary verification before acceptance.

## Correct A Run Without Teaching The Mistake

Create an editable correction document:

```powershell
python -m elliott_ai correct-run 17 --template GOOGL_RUN17_CORRECTIONS.json
```

Set `review_status` to `reviewed` only after recording at least one exact correction, rejected label, corrected hierarchy entry, or candidate general lesson. Then store the reviewed revision:

```powershell
python -m elliott_ai correct-run 17 --file GOOGL_RUN17_CORRECTIONS.json
```

Each save creates an immutable revision. A reviewed correction constrains the next degree-resolution pass. A draft is context only. General lessons remain candidates until independent blind tests support them.

## Resolve Standard Degrees And Produce The Clean Report

First assemble the no-cost resolution packet:

```powershell
python -m elliott_ai resolve-degrees 17 --provider packet --output googl_degree_packet.json
```

It must report the expected market datasets and zero unexpectedly missing zoom windows. Then run the reasoning pass:

```powershell
python -m elliott_ai resolve-degrees 17 --provider openai --output googl_degree_resolution.json
```

The resolver accepts only these degree names: Grand Supercycle, Supercycle, Cycle, Primary, Intermediate, Minor, Minute, Minuette, Subminuette, and Unassigned. It requires explicit parent/child IDs, structured dates and prices, completion state, per-wave confidence, and confirmed/candidate/unassigned status. Vague labels such as `Higher degree` fail validation.

Render the latest reasoned resolution as Markdown:

```powershell
python -m elliott_ai report 17 --output GOOGL_CLEAN_WAVE_DEGREE_REPORT.md
```

The resolution step automatically recalculates every resolved wave against its assigned unchanged timeframe. The clean report now separates model interpretation from deterministic measurements and includes:

1. Per-wave normalized volume, EWO, MACD, ATR, and volatility-band measurements.
2. Fibonacci relationships on both arithmetic and logarithmic price distance, with the selected scale disclosed.
3. Wave durations, Wave 2/4 alternation, and soft user-provided degree-duration flags.
4. Elliott 1-3/2 and 2-4/1 channel deviations on arithmetic and logarithmic scales.
5. Optional context calculations and explicit unavailable-data notices.

For a resolution created before these calculators existed, the `report` command computes the analytical sections in memory from the saved boundaries and any still-available OHLCV files. It does not make another paid model call.

Optional context files can be added to the initial analysis with repeated `--context` arguments. Exact JSON formats are documented in `inputs\ANALYTICAL_CONTEXT_FORMATS.md`.

The report remains `PROVISIONAL` when any final gate fails. Final readiness requires:

1. Standard degree names and valid parent-child links.
2. Completed waves confirmed at their assigned degrees.
3. Completed non-terminal parents with proven internal subdivisions.
4. Classical impulse price rules passing deterministic checks.
5. Degree confidence of at least 75%.
6. Passed price reconciliation.
7. Same-timeframe EWO/volume aggregation for every completed verified five-wave parent.
8. No unresolved structural items.

Only then can the result become approved memory:

```powershell
python -m elliott_ai accept 17 --title "Reviewed GOOGL canonical recount" --note "Hierarchy and boundaries manually reviewed"
```

`--blind` continues to exclude approved cases so it remains an independent test. Memory-assisted analysis, without `--blind`, can retrieve approved corrected cases.

## Adaptive Blind Recount

The staged adaptive workflow is an opt-in pre-freeze analysis path. It selects
timeframes from a content-hashed provider capability set, emits typed data
requests for defined structural questions, builds independent blind Primary
and Alternative candidates, and reuses the lower-timeframe candidate generator
and deterministic subdivision verifier one degree at a time. The legacy
`analyze`, `refine`, and `resolve-degrees` commands remain available.

Inspect the available TradingView boundary and all adaptive commands:

```powershell
python -m elliott_ai adaptive-recount mcp-status
python -m elliott_ai adaptive-recount --help
```

Create the strict RKLB plan after establishing an exact completed-candle UTC
cutoff:

```powershell
python -m elliott_ai adaptive-recount plan `
  --symbol NASDAQ:RKLB `
  --start 2026-05-27T00:00:00Z `
  --cutoff <LATEST_COMPLETED_CANDLE_UTC> `
  --market-data-provider tradingview-mcp `
  --analysis-profile elliott_full `
  --max-depth 3 `
  --max-data-requests 8
```

The command is allowed to return `not_covered`. In this Codex installation the
audited TradingView tool can request chart analysis but does not expose the raw
OHLCV, completion, session, adjustment, interval-discovery, and pagination
contract required by `NativeOHLCVBundle`. The workflow therefore stops
explicitly instead of fabricating a TradingView adapter. Twelve Data remains a
separate provider and is never mixed silently with TradingView.

Continue a supported run through the file-only stages shown by each command's
`--help`: `ingest`, `generate-roots`, `plan-proof`, `prove`, `ready`, and
`report`. Only `finalize` may enter the normal stored analysis and
`resolve_degrees()` path, and it requires `--allow-model-call --commit`.

`elliott_full` enables RSI, EWO, MACD, volume, volatility, and scale diagnostics
for the adaptive path. The legacy feature defaults remain unchanged. All
indicators are soft evidence attached only after price boundaries exist.

The complete contracts, safety gates, and Model B Codex ingestion workflow are
in `docs/adaptive_elliott_recount_spec.md`. The repository skill
`$elliott-adaptive-recount` runs the staged loop.

Natural-language Codex request:

> Run a strict adaptive blind recount of RKLB from 27 May 2026 through the
> latest completed candle using TradingView MCP. Use the elliott_full profile,
> independently generate Primary and Alternative counts, dynamically request
> any useful provider-supported timeframe, recursively prove important parent
> waves from lower-timeframe native candles, and return confirmation,
> invalidation and count-switch conditions. Stop explicitly if TradingView MCP
> cannot supply provenance-complete raw OHLCV.

## TradingView MCP Contract

Any future raw-candle TradingView adapter must pass the same normalized fields already accepted by the agent:

```json
[
  {
    "date": "2026-07-17",
    "open": 346.0,
    "high": 348.52,
    "low": 341.36,
    "close": 346.77,
    "volume": 29960000
  }
]
```

The MCP layer supplies candles and chart actions; it is never the wave-reasoning
layer. The currently exposed tool is
`mcp__codex_apps__tradingcursor_request_analysis`, whose documented result is
insufficient for raw-candle ingestion. Until a provenance-complete OHLCV tool
is available, the adapter remains unavailable and the engine reports that
boundary rather than claiming a live TradingView feed.

## Important Boundary

This MVP organizes evidence and enforces consistency. It is not an autonomous trading system. It contains no order execution, entry/exit recommendation, stop/target generator, or position-sizing module. Before relying on any count, build a blind historical evaluation set and measure invalidation and relabel frequency.
