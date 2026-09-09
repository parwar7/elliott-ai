# Deterministic Chart Annotation Workflow

## Purpose

The chart-annotation layer turns an already analysed Elliott wave map into a
reproducible TradingView Pine overlay. It does not discover pivots, choose a
count, calculate indicators, or verify Elliott structure.

The normal flow is:

```text
closed-candle evidence
  -> independent Primary and Alternative analysis
  -> deterministic structural validation
  -> immutable ChartAnnotationPlan
  -> deterministic Pine v6 rendering
  -> TradingView compile and visual QA
```

## Contracts

`elliott_ai.chart_annotations` provides:

- `ChartSourceMetadata`: symbol, feed, session, adjustment, cutoff and dataset
  hashes.
- `ChartPivot`: one catalog-backed timestamp and price with source lineage.
- `ChartLayer`: detail view, timeframe visibility and label color.
- `ChartLabelDraft`: a proposed label before deterministic stacking.
- `ChartPattern`: explicit diagonal, triangle, wedge or channel geometry.
- `ChartAnnotationPlan`: the immutable, canonical-hashed rendering input.

The plan supports one Primary hypothesis and one optional Alternative. Shared,
Primary and Alternative marks remain distinguishable in the generated Pine
script.

## Safety Rules

- Every pivot must occur at or before the frozen cutoff.
- Every pivot must reference one of the plan's dataset hashes.
- Unknown pivot, layer and hypothesis references are rejected.
- Candidate and unproven label text cannot claim `verified` or `confirmed`.
- The renderer adds `?` to candidate and unproven labels when needed.
- A verified label or pattern requires deterministic verification lineage and
  a SHA-256 verification hash.
- Candidate and unproven pattern lines are dashed. Verified lines are solid.
- Ordinary wave labels never create connector lines.
- The rendering layer never promotes a status or applies Elliott hard rules.

## Label Stacking

Labels with the same `stack_group` and placement are assigned unique vertical
positions. When `stack_order` is omitted, smaller degrees are placed nearest
the candle and larger degrees are placed farther away. Use the same explicit
`stack_group` when different timestamps will occupy the same candle on the
intended chart timeframe.

## Timeframe Visibility

Each layer defines `min_timeframe` and `max_timeframe` using Pine timeframe
codes. For example, `15` through `240` keeps lower-timeframe labels visible on
15-minute, 30-minute, 45-minute, 1-hour, 2-hour, 3-hour and 4-hour charts.

`detail_view` creates a manual selector in addition to automatic timeframe
visibility. The generated overlay always includes `Auto` and `All` modes.

## Pattern Geometry

- Diagonal, triangle and wedge: `alternating_boundaries` draws the first-third
  and second-fourth boundaries through the final pattern time.
- Channel: `one_three_parallel_two` or `two_four_parallel_one` makes the
  channel construction explicit.
- The supplied pivots must be unique and strictly time ordered.
- Pattern geometry is presentation evidence, not structural verification.

## Rendering

Create a plan in Python with `ChartAnnotationPlan.create(...)`, serialize
`plan.to_dict()` as JSON, then run:

```powershell
python scripts\render_chart_annotation_plan.py `
  path\to\annotation_plan.json `
  path\to\generated_overlay.pine
```

The command validates the plan hash and writes the Pine file atomically. It
refuses to replace different content unless `--overwrite` is supplied. Its JSON
summary reports the plan hash, render hash and Pine source hash.

The application CLI, analysis pipeline and SQLite database are not involved.

## TradingView QA

After loading the generated Pine source:

1. Confirm the plan ID and hash in the script header.
2. Inspect Macro, Current and All detail views as applicable.
3. Inspect Primary, Alternative and Both count modes.
4. Check label side and stack order at shared candles.
5. Check pattern boundaries against their catalog pivots.
6. Confirm candidate lines are dashed and verified lines are solid.
7. Confirm ordinary waves have labels without connector lines.
8. Save the TradingView layout only after the script compiles cleanly.
