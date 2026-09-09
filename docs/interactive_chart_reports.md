# Interactive Chart Reports

`elliott_ai.interactive_chart_renderer` provides an offline HTML companion to
the existing Markdown/Pine presentation. It does not run analysis or access
SQLite. Rendering requires the optional `plotly` Python package; the HTML embeds
the Plotly runtime and candle data and needs no server or network connection.

The TSLA prototype is built with the bundled Python runtime using
`python -m scripts.build_tsla_interactive_report`. Its output directory must not
already contain the output files. The prototype reuses the September 9 saved
dataset and candidate labels discussed in the report. It is not a new recount.

Public functions:

- `load_report_datasets(directory, manifest)`: verify file SHA-256 hashes, row
  counts, OHLCV relationships, ordering, cutoff, and completed-candle declarations.
- `render_interactive_report(plan, datasets, notes=(), levels=())`: validate the
  existing ChartAnnotationPlan, source lineage, and exact source pivot membership,
  then return self-contained HTML. Levels are presentation annotations supplied
  by the caller; they are not calculated or structurally verified by the renderer.

The chart offers six saved timeframes, independent count filtering, degree
filtering, arithmetic/logarithmic scale, volume, and selectable RSI/EWO/MACD.
Candidate labels retain question marks. Clicking a label displays its source
pivot and status. Labels on a shared displayed candle stack on their respective
sides. Intraday mapping of a daily pivot requires a unique matching native
extremum on that date; ambiguous or unavailable mappings are omitted and counted
visibly. This placement does not assert intrabar chronology.

Limitations: prices are frozen snapshots, no live refresh or automatic recount;
no recursive proof is performed; no inferred target projections; no hierarchy
navigation or pattern/channel rendering in this first prototype. Plans containing
patterns must continue through the existing Pine renderer until HTML geometry is
implemented. Full historical labels are not invented where the discussion only
supplied a current candidate map. Source session and adjustment declarations are
preserved rather than independently reacquired or revalidated with a provider.

Verification: `python -m unittest scripts.test_interactive_chart_renderer
scripts.test_chart_annotations`; `node scripts/check_interactive_report.cjs`
uses bundled Playwright with installed Edge for desktop/mobile checks.
