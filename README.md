# Elliott AI

Private source-review copy of the Elliott AI project prepared September 9, 2026.
Start with `AGENTS.md`, `ELLIOTT_AI_README.md`, and `AI_HANDOFF_GUIDE.md`.

## Source map

- `elliott_ai/`: analysis, deterministic validation, indicators, providers,
  persistence contracts, forecast/outcome components, and report renderers.
- `scripts/`: utilities, tests, and historical operational scripts.
- `docs/`: specifications and workflow documentation.
- `.agents/skills/`: repository-specific recount and outcome-review workflows.
- `AI_BRAIN_MASTER_RULES.json`, `AI_BRAIN_CURRENT.md`: existing knowledge sources.
- Root reports and completion documents: historical context, not verified facts
  or permission to reuse prior counts in blind generation.

## Review boundaries

This is a source review copy, not a complete runtime/data backup. Private SQLite
records, credentials, raw market data, generated reports directories, installed
dependencies, and local session state are excluded. Database schema and
persistence methods remain available in `elliott_ai/knowledge.py`.

Do not start API calls, analysis, ingestion, indexing, migrations, Telegram,
TradingView actions, or scheduled work merely to review this repository.
Existing historical reports and local absolute paths may be stale. Consult the
implementation and tests before treating documentation as current behavior.

The existing Dockerfile expects a private seed database that is intentionally
absent. Deployment needs separate configuration. Some historical scripts and
integration tests require excluded local datasets. Do not fabricate those files
or claim the full suite passes in this copy.

## Local checks

Use Python 3.12 and a virtual environment. Review and install `requirements.txt`
and any needed entries from `requirements-optional.txt`. The optional interactive
HTML renderer additionally requires Plotly; `requirements-report.txt` pins
the 7.0.0 version used for the prototype.

Pure chart-plan tests can run with:

```text
python -m unittest scripts.test_chart_annotations
```

Never place API keys in a commit. `.env.example` is a template only.

## Proposed changes

Work on a separate branch, add focused tests, and submit changes for review.
Preserve the price-first rules, independent candidate generation, deterministic
verification authority, cutoff controls, and explicit human learning gates.
Changes here do not update the original Windows working directory automatically.
