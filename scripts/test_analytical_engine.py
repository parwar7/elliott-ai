from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elliott_ai.agent import AnalysisRequest, ElliottAgent
from elliott_ai.context_data import summarize_context_file
from elliott_ai.indicators import compare_price_scales, enrich_candles
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.providers import PacketProvider
from elliott_ai.reporting import render_degree_report
from elliott_ai.wave_metrics import calculate_resolution_analytics
from scripts.test_elliott_ai import _degree_response


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _candles(count: int = 110) -> list[dict[str, object]]:
    start = datetime(2025, 12, 1, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    for index in range(count):
        date = start + timedelta(days=index)
        trend = 8.0 + index * 0.28
        price = trend + math.sin(index / 4.0) * 1.2
        rows.append(
            {
                "date": date.date().isoformat(),
                "open": price - 0.15,
                "high": price + 0.65,
                "low": price - 0.7,
                "close": price,
                "volume": 1_000_000 + (index % 11) * 45_000,
            }
        )
    return rows


class IndicatorEngineTests(unittest.TestCase):
    def test_all_candle_features_are_calculated(self) -> None:
        rows = enrich_candles(_candles())
        latest = rows[-1]
        self.assertIsNotNone(latest["ewo"])
        self.assertIsNotNone(latest["ewo_normalized_pct"])
        self.assertIsNotNone(latest["macd"])
        self.assertIsNotNone(latest["macd_signal"])
        self.assertIsNotNone(latest["macd_histogram"])
        self.assertIsNotNone(latest["atr_pct"])
        self.assertIsNotNone(latest["bollinger_position"])
        self.assertIsNotNone(latest["keltner_position"])
        self.assertIsNotNone(latest["volume_ratio_50"])

    def test_exponential_series_prefers_log_scale(self) -> None:
        rows = [
            {"close": math.exp(index * 0.03)}
            for index in range(1, 121)
        ]
        result = compare_price_scales(rows)
        self.assertEqual(result["preferred_scale"], "logarithmic")
        self.assertLess(
            result["logarithmic"]["mean_absolute_percentage_error"],
            result["arithmetic"]["mean_absolute_percentage_error"],
        )


class ContextAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _summary(self, name: str, value: object) -> dict[str, object]:
        path = self.workspace / name
        _write_json(path, value)
        return summarize_context_file(path)

    def test_yield_options_and_order_book_calculations(self) -> None:
        yields = self._summary(
            "yields.json",
            {
                "context_type": "yields",
                "rows": [
                    {"date": "2026-07-18", "yield_2y": 4.1, "yield_10y": 4.4},
                    {"date": "2026-07-19", "yield_2y": 4.0, "yield_10y": 4.5},
                ],
            },
        )
        self.assertAlmostEqual(yields["summary"]["latest"]["spread_10y_2y"], 0.5)

        options = self._summary(
            "options.json",
            {
                "context_type": "options",
                "underlying_price": 100,
                "contracts": [
                    {"type": "call", "strike": 100, "volume": 20, "open_interest": 50, "iv": 0.25, "gamma": 0.02},
                    {"type": "put", "strike": 95, "volume": 30, "open_interest": 75, "iv": 0.3, "gamma": 0.01},
                ],
            },
        )
        self.assertAlmostEqual(options["summary"]["put_call_volume_ratio"], 1.5)
        self.assertGreater(options["summary"]["gross_unsigned_gamma_exposure"], 0)

        book = self._summary(
            "book.json",
            {
                "context_type": "order_book",
                "bids": [[99.9, 100], [99.8, 50]],
                "asks": [[100.1, 80], [100.2, 40]],
            },
        )
        self.assertAlmostEqual(book["summary"]["spread"], 0.2)
        self.assertGreater(book["summary"]["depth_imbalance"], 0)


class ResolutionAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.daily = self.workspace / "tv_test_daily.json"
        _write_json(self.daily, _candles())
        self.response = _degree_response(7)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_post_boundary_metrics_cover_waves_ratios_duration_and_channels(self) -> None:
        metrics = calculate_resolution_analytics(
            self.response, [self.daily], workspace=self.workspace
        )
        calculated = [
            item for item in metrics["wave_features"]
            if item["status"] == "calculated"
        ]
        self.assertEqual(len(calculated), 6)
        self.assertEqual(len(metrics["fibonacci_duration_alternation"]), 1)
        sequence = metrics["fibonacci_duration_alternation"][0]
        self.assertEqual(sequence["ratio_scale"], "logarithmic")
        self.assertIn("wave_3_extension_of_wave_1", sequence["ratios"])
        self.assertEqual(len(metrics["fibonacci_reference_levels"]), 1)
        self.assertGreaterEqual(
            len(metrics["fibonacci_reference_levels"][0]["references"]), 4
        )
        self.assertEqual(len(metrics["duration_prior_checks"]), 6)
        self.assertEqual(len(metrics["elliott_channels"]), 1)

    def test_report_exposes_calculations_and_no_execution_module(self) -> None:
        metrics = calculate_resolution_analytics(
            self.response, [self.daily], workspace=self.workspace
        )
        run = {
            "id": 7,
            "symbol": "TEST",
            "evidence": {
                "market_data": [],
                "context_data": [],
                "data_freshness": {},
            },
        }
        resolution = {
            "id": 3,
            "provider": "test",
            "model": "fixture",
            "response": self.response,
            "readiness": {
                "final_report_ready": False,
                "reconciliation_status": "passed",
                "completed_wave_count": 6,
                "impulse_sequences_checked": 1,
                "blockers": [],
                "warnings": [],
                "deterministic_impulse_verification": [],
                "analytical_metrics": metrics,
            },
        }
        report = render_degree_report(run, resolution)
        self.assertIn("## Per-Wave Indicator Measurements", report)
        self.assertIn("## Fibonacci, Duration And Alternation", report)
        self.assertIn("## Fibonacci Reference Levels", report)
        self.assertIn("not trade targets", report)
        self.assertIn("## Elliott Channel Comparison", report)
        self.assertNotIn("Position Size", report)
        self.assertNotIn("Trade Entry", report)


class AgentFeaturePacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        _write_json(self.workspace / "AI_BRAIN_MASTER_RULES.json", {"rule": "Price first."})
        (self.workspace / "AI_BRAIN_CURRENT.md").write_text("# Brain\nPrice first.\n", encoding="utf-8")
        _write_json(self.workspace / "tv_test_daily.json", _candles())
        _write_json(
            self.workspace / "test_analysis_context.json",
            {
                "context_type": "yields",
                "rows": [{"date": "2026-07-19", "yield_2y": 4.0, "yield_10y": 4.5}],
            },
        )
        self.store = KnowledgeStore(self.workspace / "brain.sqlite3")
        self.store.index_workspace(self.workspace)
        self.assertEqual(self.store.stats()["documents"], 2)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_feature_selection_and_context_are_evidence_bound(self) -> None:
        agent = ElliottAgent(
            workspace=self.workspace,
            store=self.store,
            provider=PacketProvider(),
        )
        packet = agent.build_evidence_packet(
            AnalysisRequest(
                question="Analyze TEST with MACD only.",
                symbol="TEST",
                features=("macd",),
                context_paths=("test_analysis_context.json",),
            )
        )
        self.assertEqual(packet["feature_manifest"]["active_candle_features"], ["macd"])
        columns = packet["market_data"][0]["analysis_views"]["recent_native"]["columns"]
        self.assertIn("macd_histogram", columns)
        self.assertNotIn("ewo", columns)
        self.assertEqual(packet["context_data"][0]["evidence_id"], "C1")
        self.assertEqual(packet["context_data"][0]["summary"]["status"], "calculated")


if __name__ == "__main__":
    unittest.main()
