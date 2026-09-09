# Elliott AI Handoff Guide

This workspace contains the complete local Elliott Wave AI project built in this
Codex task. It is an evidence-bound analysis agent, not an autonomous trading
system and not a foundation model trained from scratch.

## What To Read First

1. `AI_BRAIN_CURRENT.md` - concise description of the active method.
2. `AI_BRAIN_MASTER_RULES.json` - canonical machine-readable rule database.
3. `inputs/Elliott_Wave_AI_Training_Manual_V3_Master_Copy_Paste.txt` - detailed
   training manual.
4. `inputs/Elliott_Wave_AI_Rulebook_Copy_Paste.txt` - copy/paste rulebook.
5. `ELLIOTT_AI_README.md` - commands, architecture, review workflow, and limits.
6. `elliott_ai/` - the Python LLM agent, retrieval, indicators, validation,
   degree resolution, reporting, and provider integrations.
7. `scripts/` - deterministic analytics, tests, TradingView capture bridge, and
   historical recount/report utilities.
8. `.elliott_ai/elliott_ai.sqlite3` - local indexed knowledge, analysis runs,
   reviews, degree resolutions, and approved-memory tables.

## Meaning Of "The LLM"

No OpenAI model weights are stored in this workspace. OpenAI models run on
OpenAI servers. The transferable LLM system consists of:

- the OpenAI Responses API and Ollama provider integrations;
- the system prompts and JSON response contracts in `elliott_ai/agent.py`;
- the canonical brain and training documents;
- deterministic price, Fibonacci, duration, volume, EWO, MACD, volatility,
  scale, channel, correction, and impulse calculations;
- the SQLite retrieval and reviewed-memory system;
- saved evidence packets, model outputs, reports, and tests.

To run fully offline, install Ollama separately and download a local model. Its
weights are managed by Ollama and are not part of these archives.

## Current Memory State At Packaging

The local database reported:

- 59 indexed documents;
- 696 searchable chunks;
- 18 analysis runs;
- 3 degree resolutions;
- 0 reviewed corrections;
- 0 accepted cases.

This means the agent has a substantial rule and run history, but no chart count
has yet passed the explicit human-review and acceptance gate into reusable case
memory. Previous reports are evidence and hypotheses, not automatically truth.

## Source Priority

Use this order when files disagree:

1. Classical Elliott hard price and structure rules.
2. `AI_BRAIN_MASTER_RULES.json` and `AI_BRAIN_CURRENT.md`.
3. Deterministic calculations from unchanged OHLCV data.
4. Reviewed and explicitly accepted case memory.
5. Provisional reports and historical analysis outputs.
6. Original source attachments.

The files in `source_attachments/` are historical inputs. Some contain older
rules, including mandatory RSI claims, that were later corrected. They must not
override the current canonical brain without a documented rule audit.

## Active Analysis Method

- Build counts from the highest timeframe downward.
- Lock price pivots and required internal structures before indicators.
- Enforce parent-child date and price boundaries recursively.
- Test at least one serious alternate when the hierarchy is ambiguous.
- Inspect arithmetic and logarithmic scale where appropriate.
- Use Fibonacci, duration, alternation, channels, volume, EWO, MACD, and
  volatility to rank price-valid counts.
- RSI is disabled by default and becomes optional evidence only when explicitly
  enabled. It never overrides price structure or acts as a hard invalidation.
- Generate versioned wave fingerprints only from candles available at the
  declared cutoff. Fingerprints describe supplied candidates and never relabel
  them, create probabilities, or use future candles.
- Compare only structurally paired fingerprints. Preserve unavailable ratios as
  null and quarantine volume comparisons across incompatible feeds or venues.
- Keep feed, session, adjustment, and timeframe metadata attached to every
  measurement. Do not compare incompatible volume feeds.
- Mark missing child proof as `Unproven Internal Structure` instead of forcing
  certainty.

## Basic Commands

From PowerShell in the project directory:

```powershell
python -m elliott_ai stats
python -m elliott_ai index
python -m elliott_ai analyze "Strict blind recount" --symbol NASDAQ:GOOGL --blind --provider packet
python -m unittest discover -s scripts -p "test_*.py"
```

For OpenAI, set `OPENAI_API_KEY` only in the current PowerShell session. Never
write a real key into this project or an uploaded archive.

## Suggested Prompt For Another ChatGPT

```text
Read AI_HANDOFF_GUIDE.md first, then AI_BRAIN_CURRENT.md,
AI_BRAIN_MASTER_RULES.json, ELLIOTT_AI_README.md, and the elliott_ai source.
Treat old reports as provisional evidence rather than labels to copy. Audit the
architecture, deterministic calculations, prompts, schemas, tests, and learning
gate. Identify concrete weaknesses that could cause incorrect Elliott counts.
Propose improvements as a ranked change list with exact files, tests, and
migration steps. Do not weaken hard Elliott rules, silently promote old counts,
add trade execution, or expose credentials.
```

## Package Types

- `Elliott_AI_CORE_*.zip` contains the intelligence layer, memory database,
  textual reports/data, source attachments, and code, without chart screenshots
  and render folders.
- `Elliott_AI_FULL_*.zip` contains every accessible project artifact except Git
  metadata, third-party dependency caches, temporary lock files, bytecode, and
  previously generated handoff archives.

Each ZIP includes `_PACKAGE_MANIFEST.csv` with relative paths, sizes, timestamps,
and SHA-256 hashes.
