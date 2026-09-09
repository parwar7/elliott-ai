"""Presentation tests: source integrity, status safety, and deterministic output."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from elliott_ai.chart_annotations import ChartAnnotationPlan, ChartSourceMetadata, ChartHypothesis, ChartLayer, ChartPivot, ChartLabelDraft
from elliott_ai.interactive_chart_renderer import load_report_datasets, render_interactive_report


class InteractiveReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.dataset = {'metadata': dict(provider='twelve_data', provider_symbol='TEST', exchange='NASDAQ',
            session='regular', adjustment='splits', dividend_adjustment='none',
            completed_candles_only=True, applicable_cutoff='2026-01-02T21:00:00+00:00',
            captured_at='2026-01-03T00:00:00+00:00'),
            'candles': [dict(date='2026-01-02T14:30:00+00:00', open=10, high=12, low=9, close=11, volume=100)]}
        self.save()

    def save(self):
        raw=json.dumps(self.dataset).encode()
        (self.directory/'daily.json').write_bytes(raw)
        self.sha=hashlib.sha256(raw).hexdigest()
        self.manifest={'files':[dict(path='daily.json',timeframe='daily',sha256=self.sha,count=1)]}

    def plan(self):
        return ChartAnnotationPlan.create(plan_id='test',title='Test',
            source=ChartSourceMetadata('NASDAQ:TEST','NASDAQ','twelve_data','test','regular','America/New_York',
                'splits','OHLC','2026-01-03T00:00:00+00:00',{'daily':self.sha}),
            hypotheses=[ChartHypothesis('primary','Primary','primary')],
            layers=[ChartLayer('current','Current','Current','15','1M','#1466AE')],
            pivots=[ChartPivot('p1',self.dataset['candles'][0]['date'],12,'high','daily',self.sha)],
            label_drafts=[ChartLabelDraft('l1','p1','current','1','Minor','above','unproven','primary')])

    def test_hash_tampering(self):
        (self.directory/'daily.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            load_report_datasets(self.directory,self.manifest)

    def test_invalid_prices(self):
        self.dataset['candles'][0]['high']=8
        self.save()
        with self.assertRaisesRegex(ValueError,'relationship'):
            load_report_datasets(self.directory,self.manifest)

    def test_cutoff(self):
        self.dataset['metadata']['applicable_cutoff']='2026-01-01T00:00:00+00:00'
        self.save()
        with self.assertRaisesRegex(ValueError,'cutoff'):
            load_report_datasets(self.directory,self.manifest)

    def test_provenance(self):
        ds=load_report_datasets(self.directory,self.manifest)
        ds['daily']['file_sha256']='a'*64
        with self.assertRaisesRegex(ValueError,'lineage'):
            render_interactive_report(self.plan(),ds)

    def test_pivot_membership(self):
        ds=load_report_datasets(self.directory,self.manifest)
        ds['daily']['candles'][0]['high']=13
        with self.assertRaisesRegex(ValueError,'backed'):
            render_interactive_report(self.plan(),ds)

    def test_determinism_and_no_promotion(self):
        ds=load_report_datasets(self.directory,self.manifest)
        before=copy.deepcopy(ds)
        a=render_interactive_report(self.plan(),ds,notes=['</script><script>alert(1)</script>'])
        b=render_interactive_report(self.plan(),ds,notes=['</script><script>alert(1)</script>'])
        self.assertEqual(a,b)
        self.assertEqual(ds,before)
        self.assertIn('"display_text":"1?"',a)
        self.assertNotIn('</script><script>alert(1)',a)


if __name__=='__main__':
    unittest.main()
