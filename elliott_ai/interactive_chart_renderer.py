"""Offline HTML presentation of immutable chart plans and saved candle files."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from .chart_annotations import ChartAnnotationPlan
from .pine_overlay_renderer import status_safe_label_text


def load_report_datasets(directory: Path, manifest: dict) -> dict:
    """Verify byte hashes before reading saved OHLCV; never fetch or repair data."""
    result = {}
    for entry in manifest['files']:
        path = (directory / entry['path']).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError('Dataset path escapes source directory')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Dataset hash mismatch: ' + entry['timeframe'])
        dataset = json.loads(raw)
        rows = dataset['candles']
        if not rows or len(rows) != entry['count']:
            raise ValueError('Dataset count mismatch')
        metadata = dataset['metadata']
        if metadata.get('completed_candles_only') is not True:
            raise ValueError('Dataset must declare completed candles')
        times = [datetime.fromisoformat(c['date']) for c in rows]
        cutoff = datetime.fromisoformat(metadata['applicable_cutoff'])
        if any(a >= b for a, b in zip(times, times[1:])) or times[-1] > cutoff:
            raise ValueError('Invalid candle ordering or cutoff')
        for c in rows:
            if any(not isinstance(c[k], (float, int)) or isinstance(c[k], bool)
                   or not math.isfinite(c[k]) for k in ('open', 'high', 'low', 'close', 'volume')):
                raise ValueError('Invalid OHLCV value')
            if c['volume'] < 0 or c['low'] > min(c['open'], c['close']) or c['high'] < max(c['open'], c['close']) or c['low'] > c['high']:
                raise ValueError('Invalid OHLCV relationship')
        result[entry['timeframe']] = dict(dataset, file_sha256=entry['sha256'])
    return result


def render_interactive_report(plan: ChartAnnotationPlan, datasets: dict, *, notes=(), levels=()) -> str:
    """Return a self-contained document. Plotly is an optional rendering dependency."""
    from plotly.offline import get_plotlyjs

    plan = ChartAnnotationPlan.from_dict(plan.to_dict())
    if plan.patterns:
        raise ValueError('HTML pattern geometry is not supported in this prototype; use the Pine renderer')
    for tf, dataset in datasets.items():
        if plan.source.dataset_hashes.get(tf) != dataset['file_sha256']:
            raise ValueError('Plan and dataset lineage mismatch')
        m = dataset['metadata']
        if (m['provider'] != plan.source.provider or m['exchange'] != plan.source.exchange
                or m['session'] != plan.source.session or m['adjustment'] != 'splits'
                or m['dividend_adjustment'] != 'none'
                or m['provider_symbol'] != plan.source.symbol.split(':')[-1]
                or datetime.fromisoformat(m['captured_at']) > datetime.fromisoformat(plan.source.cutoff)):
            raise ValueError('Incompatible dataset source')
    pivots = {p.pivot_id: p for p in plan.pivots}
    for p in plan.pivots:
        rows = datasets[p.source_timeframe]['candles']
        if not any(c['date'] == p.timestamp and c[p.kind.value] == p.price for c in rows):
            # Contracts normalize Z to +00:00; compare instants, not spellings.
            if not any(datetime.fromisoformat(c['date']) == datetime.fromisoformat(p.timestamp)
                       and c[p.kind.value] == p.price for c in rows):
                raise ValueError('Pivot is not backed by its source candle')
    payload = dict(plan=plan.to_dict(), datasets=datasets, notes=list(notes), levels=list(levels))
    payload['labels'] = [dict(l.to_dict(), display_text=status_safe_label_text(l),
                              pivot=pivots[l.pivot_id].to_dict()) for l in plan.labels]
    serialized = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    template = Path(__file__).with_name('interactive_chart_template.html').read_text(encoding='utf-8')
    return template.replace('__PLOTLY__', get_plotlyjs()).replace('__PAYLOAD__', serialized.replace('<', '\\u003c'))
