"""Render the September 9 TSLA snapshot and previously discussed candidate map."""
import hashlib
import json
from pathlib import Path

from elliott_ai.chart_annotations import (
    ChartAnnotationPlan, ChartSourceMetadata, ChartHypothesis, ChartLayer,
    ChartPivot, ChartLabelDraft,
)
from elliott_ai.interactive_chart_renderer import load_report_datasets, render_interactive_report

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'analysis_input/2026-09-09_tsla_fresh/NASDAQ_TSLA_20260909T103934Z'
OUTPUT = ROOT / 'reports/TSLA_interactive_20260909_v2'


def main():
    manifest = json.loads((SOURCE / 'TSLA_manifest.json').read_text())
    datasets = load_report_datasets(SOURCE, manifest)
    hashes = {tf: ds['file_sha256'] for tf, ds in datasets.items()}
    source = ChartSourceMetadata(
        symbol=manifest['symbol'], exchange='NASDAQ', provider='twelve_data',
        feed='Twelve Data NASDAQ (XNGS)', session='regular', timezone='America/New_York',
        adjustment='split-adjusted, dividend-unadjusted', price_basis='OHLC',
        cutoff=manifest['captured_at'], dataset_hashes=hashes,
    )
    # Presentation of the conversation's candidates, not a new wave evaluation.
    marks = [
        ('primary', '2026-04-07', 'low', 'A', 'Intermediate'),
        ('primary', '2026-05-13', 'high', 'B', 'Intermediate'),
        ('primary', '2026-07-29', 'low', 'C', 'Intermediate'),
        ('alternative', '2026-08-04', 'high', '1', 'Minor'),
        ('alternative', '2026-08-06', 'low', '2', 'Minor'),
        ('alternative', '2026-09-03', 'high', '3', 'Minor'),
        ('alternative', '2026-09-04', 'low', '4', 'Minor'),
    ]
    pivots, labels = [], []
    for i, (role, day, kind, text, degree) in enumerate(marks):
        row, = [c for c in datasets['daily']['candles'] if c['date'].startswith(day)]
        pivots.append(ChartPivot('p'+str(i), row['date'], row[kind], kind, 'daily', hashes['daily']))
        labels.append(ChartLabelDraft('l'+str(i), 'p'+str(i), role, text, degree,
                                     'above' if kind=='high' else 'below', 'candidate', role))
    plan = ChartAnnotationPlan.create(
        plan_id='tsla_snapshot_20260909', title='TSLA | Elliott wave report', source=source,
        hypotheses=[ChartHypothesis('primary', 'ABC candidate', 'primary'),
                    ChartHypothesis('alternative', 'Rebound candidate', 'alternative')],
        layers=[ChartLayer('primary', 'ABC candidate', 'Current', '15', '1M', '#1466AE'),
                ChartLayer('alternative', 'Rebound candidate', 'Current', '15', '1M', '#AC387B')],
        pivots=pivots, label_drafts=labels,
    )
    notes = [
        'Snapshot: September 8, 2026 daily close $368.16. Monthly data ends in August. Prices do not refresh automatically.',
        'Primary discussion count: A to $337.24, B to $453.40, and a possible C ending at $297.38. The larger corrective structure remains unproven.',
        'Alternative discussion count: a possible motive rebound from $297.38, with candidate 1-4 pivots. A fifth wave is a possibility; no future pivot is drawn.',
        'Degrees and endpoint labels are provisional annotations from the discussion. No recursive verification was performed for this presentation.',
        'Above $384.04 would support renewed upside. Below $351.32 challenges the proposed wave-4 ending. Below $329.57 creates overlap for the proposed regular impulse. Below $297.38 breaks the rebound origin.',
        'Indicators are displayed from each saved timeframe. They do not verify a count. Targets are omitted because no typed, validated target projection is supplied.',
    ]
    levels = [dict(role='alternative', price=p, label=l) for p,l in [
        (384.04, 'Upside trigger'), (351.32, 'W4 endpoint'),
        (329.57, 'Impulse overlap'), (297.38, 'Origin invalidation')]]
    document = render_interactive_report(plan, datasets, notes=notes, levels=levels)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    outputs = {'TSLA_report.html': document, 'TSLA_annotation_plan.json': json.dumps(plan.to_dict(), indent=2)}
    for name, text in outputs.items():
        path = OUTPUT / name
        with path.open('x', encoding='utf-8', newline='\n') as f:
            f.write(text)
    record = {'source_manifest': str(SOURCE / 'TSLA_manifest.json'), 'dataset_hashes': hashes,
              'annotation_plan_hash': plan.content_hash,
              'files': {name: hashlib.sha256((OUTPUT/name).read_bytes()).hexdigest() for name in outputs}}
    with (OUTPUT / 'render_manifest.json').open('x', encoding='utf-8') as f:
        json.dump(record, f, indent=2)
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
