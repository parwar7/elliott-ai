# Elliott AI repository guidance

Read and follow `AGENTS.md` before making changes. Consult the relevant
specification in `docs/` and repository workflows in `.agents/skills/`.

Use `.agents/skills/elliott-adaptive-recount/SKILL.md` for adaptive recount work
and `.agents/skills/elliott-outcome-review/SKILL.md` for Phase 11/outcome work.

Preserve technical independence, immutable records, deterministic hard rules,
and human approval requirements. General rules and soft indicator evidence must
remain separate from prior-symbol reports and reviewed outcomes. Never feed
prior counts or lessons into blind candidate generation.

This checkout excludes production databases, candles, credentials, and local
runtime state. Read `README.md` for limitations. Do not start model calls,
market-data requests, database operations, messaging, or production workflows
without an explicit task requesting them. Report unavailable tests honestly.

Use review branches and focused tests. Do not silently rewrite historical
records, activate lessons, or promote candidate waves to verified status.
