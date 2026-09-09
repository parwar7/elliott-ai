from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from elliott_ai.cli import build_parser
from elliott_ai.config import Settings
from elliott_ai.indicators import (
    DEFAULT_CANDLE_FEATURES,
    RSI_METHOD_ID,
    calculate_ewo,
    calculate_macd,
    calculate_wilder_rsi,
    compare_rsi_pivots,
    enrich_candles,
    normalize_features,
    with_optional_rsi,
)
from elliott_ai.market_data import load_candles, summarize_market_file
from elliott_ai.reporting import render_degree_report
from elliott_ai.schema import RSI_EVIDENCE_SCHEMA, validate_degree_resolution_shape
from elliott_ai.wave_metrics import aggregate_wave_features, calculate_resolution_analytics
from scripts.test_elliott_ai import _degree_response


WILDER_REFERENCE_CLOSES = [
    44.34,
    44.09,
    44.15,
    43.61,
    44.33,
    44.83,
    45.10,
    45.42,
    45.84,
    46.08,
    45.89,
    46.03,
    45.61,
    46.28,
    46.28,
    46.00,
    46.03,
    46.41,
    46.22,
    45.64,
]


def _candles(count: int = 70) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    for index in range(count):
        close = 100.0 + index * 0.35 + math.sin(index / 2.7) * 2.0
        rows.append(
            {
                "date": (start + timedelta(days=index)).date().isoformat(),
                "open": close - 0.25,
                "high": close + 0.8,
                "low": close - 0.9,
                "close": close,
                "volume": 1_000_000 + index * 2_500,
            }
        )
    return rows


def _source(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "provider": "tradingview",
        "venue": "NASDAQ",
        "symbol": "NASDAQ:TEST",
        "timeframe": "daily",
        "price_field": "close",
        "period": 14,
        "calculation_method": RSI_METHOD_ID,
        "feed_identity": "tradingview|NASDAQ|NASDAQ:TEST|daily|regular",
    }
    source.update(overrides)
    return source


def _pivot(
    wave_id: str,
    price: float,
    rsi: float,
    *,
    aligned: bool = True,
    source: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "wave_id": wave_id,
        "end_pivot_price": price,
        "value_at_wave_end": rsi,
        "pivot_alignment": {"status": "aligned" if aligned else "not_aligned"},
        "source": source or _source(),
    }


def _legacy_sma(values: list[float], length: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= length:
            running -= values[index - length]
        if index >= length - 1:
            output[index] = running / length
    return output


def _legacy_ema(values: list[float], length: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) < length:
        return output
    previous = sum(values[:length]) / length
    output[length - 1] = previous
    alpha = 2.0 / (length + 1.0)
    for index in range(length, len(values)):
        previous = alpha * values[index] + (1.0 - alpha) * previous
        output[index] = previous
    return output


def _assert_optional_series_equal(
    case: unittest.TestCase,
    left: list[float | None],
    right: list[float | None],
) -> None:
    case.assertEqual(len(left), len(right))
    for actual, expected in zip(left, right):
        if expected is None:
            case.assertIsNone(actual)
        else:
            case.assertAlmostEqual(float(actual), float(expected), places=12)


class WilderRsiCalculationTests(unittest.TestCase):
    def test_known_wilder_reference_values(self) -> None:
        values = calculate_wilder_rsi(WILDER_REFERENCE_CLOSES)["values"]
        self.assertAlmostEqual(values[14], 70.46413502, places=8)
        self.assertAlmostEqual(values[15], 66.24961855, places=8)
        self.assertAlmostEqual(values[19], 57.91502067, places=8)

    def test_insufficient_warm_up_and_short_dataset(self) -> None:
        result = calculate_wilder_rsi(range(14))
        self.assertTrue(all(value is None for value in result["values"]))
        self.assertTrue(all(status == "warming_up" for status in result["statuses"]))

    def test_flat_series_returns_midline(self) -> None:
        values = calculate_wilder_rsi([50.0] * 20)["values"]
        self.assertEqual(values[14:], [50.0] * 6)

    def test_all_gain_window_returns_100(self) -> None:
        values = calculate_wilder_rsi(range(20))["values"]
        self.assertEqual(values[14:], [100.0] * 6)

    def test_all_loss_window_returns_zero(self) -> None:
        values = calculate_wilder_rsi(range(20, 0, -1))["values"]
        self.assertEqual(values[14:], [0.0] * 6)

    def test_missing_and_non_finite_inputs_reset_warm_up(self) -> None:
        for bad_value in (None, float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_value=bad_value):
                closes = list(range(100, 115)) + [bad_value] + list(range(116, 131))
                result = calculate_wilder_rsi(closes)
                self.assertEqual(result["statuses"][15], "invalid_input")
                self.assertTrue(all(value is None for value in result["values"][15:30]))
                self.assertEqual(result["values"][30], 100.0)

    def test_repeated_results_are_deterministic(self) -> None:
        first = calculate_wilder_rsi(WILDER_REFERENCE_CLOSES)
        second = calculate_wilder_rsi(WILDER_REFERENCE_CLOSES)
        self.assertEqual(first, second)


class OptionalConfigurationTests(unittest.TestCase):
    def test_rsi_is_disabled_by_default(self) -> None:
        self.assertNotIn("rsi", normalize_features(None))
        rows = enrich_candles(_candles())
        self.assertNotIn("rsi", rows[-1])
        self.assertNotIn("rsi_status", rows[-1])

    def test_legacy_settings_construction_defaults_rsi_off(self) -> None:
        settings = Settings(
            workspace=Path("."),
            database_path=Path("brain.sqlite3"),
            provider="packet",
            model=None,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
            ollama_base_url="http://localhost:11434",
        )
        self.assertFalse(settings.rsi_enabled)

    def test_explicit_enable_adds_rsi_to_defaults(self) -> None:
        features = with_optional_rsi((), enabled=True)
        self.assertEqual(features[:-1], DEFAULT_CANDLE_FEATURES)
        self.assertEqual(features[-1], "rsi")
        rows = enrich_candles(_candles(), features=features)
        self.assertIsNotNone(rows[-1]["rsi"])
        self.assertEqual(rows[-1]["rsi_status"], "ready")

    def test_disabled_output_matches_explicit_legacy_defaults(self) -> None:
        implicit = enrich_candles(_candles())
        explicit = enrich_candles(_candles(), features=DEFAULT_CANDLE_FEATURES)
        self.assertEqual(implicit, explicit)

    def test_cli_and_environment_require_explicit_enable(self) -> None:
        parser = build_parser()
        disabled = parser.parse_args(["analyze", "test"])
        enabled = parser.parse_args(["analyze", "test", "--enable-rsi"])
        self.assertFalse(disabled.enable_rsi)
        self.assertTrue(enabled.enable_rsi)
        with patch.dict(os.environ, {"ELLIOTT_ENABLE_RSI": "true"}):
            self.assertTrue(Settings.from_environment().rsi_enabled)
        with patch.dict(os.environ, {"ELLIOTT_ENABLE_RSI": "false"}):
            self.assertFalse(Settings.from_environment().rsi_enabled)


class RsiDivergenceTests(unittest.TestCase):
    def test_aligned_bullish_divergence(self) -> None:
        result = compare_rsi_pivots(
            _pivot("current", 90.0, 40.0), _pivot("prior", 100.0, 30.0)
        )
        self.assertEqual(result["comparability"], "comparable")
        self.assertEqual(result["divergence_candidate"], "bullish")
        self.assertAlmostEqual(result["divergence_strength_rsi_points"], 10.0)

    def test_aligned_bearish_divergence(self) -> None:
        result = compare_rsi_pivots(
            _pivot("current", 110.0, 60.0), _pivot("prior", 100.0, 70.0)
        )
        self.assertEqual(result["divergence_candidate"], "bearish")

    def test_unaligned_pivots_do_not_return_false_divergence(self) -> None:
        result = compare_rsi_pivots(
            _pivot("current", 90.0, 40.0, aligned=False),
            _pivot("prior", 100.0, 30.0),
        )
        self.assertEqual(result["comparability"], "incomparable")
        self.assertIsNone(result["divergence_candidate"])
        self.assertEqual(result["evidence"]["status"], "incomparable")

    def test_incompatible_feed_or_timeframe_is_incomparable(self) -> None:
        for override in (
            {"feed_identity": "other-feed"},
            {"timeframe": "4h", "feed_identity": "other-4h-feed"},
        ):
            with self.subTest(override=override):
                result = compare_rsi_pivots(
                    _pivot("current", 90.0, 40.0, source=_source(**override)),
                    _pivot("prior", 100.0, 30.0),
                )
                self.assertEqual(result["comparability"], "incomparable")
                self.assertIsNone(result["divergence_candidate"])

    def test_contradictory_rsi_evidence_never_invalidates_structure(self) -> None:
        result = compare_rsi_pivots(
            _pivot("current", 110.0, 75.0),
            _pivot("prior", 100.0, 60.0),
            expected_divergence="bearish",
        )
        self.assertEqual(result["evidence"]["status"], "contradictory")
        self.assertFalse(result["evidence"]["structural_invalidation"])


class RsiPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.market_path = self.workspace / "tv_test_daily.json"
        self.market_path.write_text(
            json.dumps(
                {
                    "metadata": {
                        "provider": "tradingview",
                        "exchange": "NASDAQ",
                        "resolved_symbol": "NASDAQ:TEST",
                        "feed_identity": "tv-nasdaq-test-regular-daily",
                    },
                    "data": _candles(),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _response(self) -> dict[str, object]:
        rows = _candles()
        return {
            "degree_hierarchy": [
                {
                    "wave_id": "W1",
                    "degree": "Minor",
                    "timeframe": "daily",
                    "start": {"date": "2026-01-21", "price": rows[20]["close"]},
                    "end": {"date": "2026-02-05", "price": rows[35]["close"]},
                }
            ]
        }

    def test_wave_rsi_start_end_min_max_and_source_metadata(self) -> None:
        result = aggregate_wave_features(
            self._response(),
            [self.market_path],
            workspace=self.workspace,
            features=("rsi",),
        )[0]
        candles = enrich_candles(load_candles(self.market_path), features=("rsi",))
        rsi = result["rsi"]
        self.assertAlmostEqual(rsi["value_at_wave_start"], candles[20]["rsi"])
        self.assertAlmostEqual(rsi["value_at_wave_end"], candles[35]["rsi"])
        expected = [row["rsi"] for row in candles[20:36]]
        self.assertAlmostEqual(rsi["minimum"], min(expected))
        self.assertAlmostEqual(rsi["maximum"], max(expected))
        self.assertEqual(rsi["pivot_alignment"]["status"], "aligned")
        self.assertEqual(rsi["source"]["provider"], "tradingview")
        self.assertEqual(rsi["source"]["timeframe"], "daily")
        self.assertEqual(rsi["source"]["price_field"], "close")
        self.assertEqual(rsi["source"]["period"], 14)

    def test_explicit_prior_wave_comparison_uses_aligned_pipeline_pivots(self) -> None:
        rows = _candles()
        response = {
            "degree_hierarchy": [
                {
                    "wave_id": "W1",
                    "degree": "Minor",
                    "timeframe": "daily",
                    "start": {"date": rows[20]["date"], "price": rows[20]["close"]},
                    "end": {"date": rows[27]["date"], "price": rows[27]["close"]},
                },
                {
                    "wave_id": "W3",
                    "degree": "Minor",
                    "timeframe": "daily",
                    "start": {"date": rows[28]["date"], "price": rows[28]["close"]},
                    "end": {"date": rows[35]["date"], "price": rows[35]["close"]},
                    "rsi_comparison_wave_id": "W1",
                },
            ]
        }
        results = aggregate_wave_features(
            response,
            [self.market_path],
            workspace=self.workspace,
            features=("rsi",),
        )
        current = next(item for item in results if item["wave_id"] == "W3")["rsi"]
        comparison = current["comparison_with_prior_wave"]
        self.assertEqual(comparison["prior_wave_id"], "W1")
        self.assertEqual(comparison["comparability"], "comparable")
        self.assertTrue(current["pivot_alignment"]["date_aligned"])
        self.assertTrue(current["pivot_alignment"]["price_aligned"])

    def test_market_summary_exposes_rsi_only_when_enabled(self) -> None:
        disabled = summarize_market_file(self.market_path)
        enabled = summarize_market_file(
            self.market_path,
            features=with_optional_rsi((), enabled=True),
        )
        self.assertNotIn("rsi", disabled["technical_features"])
        self.assertNotIn("rsi", disabled["analysis_views"]["recent_native"]["columns"])
        self.assertIn("rsi", enabled["technical_features"])
        self.assertIn("rsi", enabled["analysis_views"]["recent_native"]["columns"])
        self.assertEqual(enabled["technical_features"]["rsi"]["source"]["venue"], "NASDAQ")

    def test_reports_do_not_imply_disabled_rsi_but_show_enabled_rsi(self) -> None:
        response = _degree_response(7)
        run = {
            "id": 7,
            "symbol": "TEST",
            "evidence": {"market_data": [], "context_data": [], "data_freshness": {}},
        }
        base_readiness = {
            "final_report_ready": False,
            "reconciliation_status": "passed",
            "completed_wave_count": 6,
            "impulse_sequences_checked": 1,
            "blockers": [],
            "warnings": [],
            "deterministic_impulse_verification": [],
        }
        disabled_metrics = calculate_resolution_analytics(
            response,
            [self.market_path],
            workspace=self.workspace,
            features=DEFAULT_CANDLE_FEATURES,
        )
        enabled_metrics = calculate_resolution_analytics(
            response,
            [self.market_path],
            workspace=self.workspace,
            features=with_optional_rsi((), enabled=True),
        )
        disabled_report = render_degree_report(
            run,
            {
                "id": 1,
                "provider": "test",
                "model": "fixture",
                "response": response,
                "readiness": {**base_readiness, "analytical_metrics": disabled_metrics},
            },
        )
        enabled_report = render_degree_report(
            run,
            {
                "id": 2,
                "provider": "test",
                "model": "fixture",
                "response": response,
                "readiness": {**base_readiness, "analytical_metrics": enabled_metrics},
            },
        )
        self.assertNotIn("RSI", disabled_report)
        self.assertIn("RSI start/end", enabled_report)
        self.assertIn("do not reduce readiness", enabled_report)

    def test_old_schema_and_new_optional_comparison_reference_are_readable(self) -> None:
        old_response = _degree_response(7)
        self.assertEqual(validate_degree_resolution_shape(old_response, source_run_id=7), [])
        old_response["degree_hierarchy"][5]["rsi_comparison_wave_id"] = "I3"
        self.assertEqual(validate_degree_resolution_shape(old_response, source_run_id=7), [])
        self.assertIn("source", RSI_EVIDENCE_SCHEMA["properties"])


class CanonicalCompatibilityTests(unittest.TestCase):
    def test_ewo_matches_previous_sma_implementation(self) -> None:
        highs = [100.0 + index * 0.7 + math.sin(index) for index in range(80)]
        lows = [value - 2.5 for value in highs]
        medians = [(high + low) / 2.0 for high, low in zip(highs, lows)]
        fast = _legacy_sma(medians, 5)
        slow = _legacy_sma(medians, 35)
        expected = [
            a - b if a is not None and b is not None else None
            for a, b in zip(fast, slow)
        ]
        actual = calculate_ewo(highs, lows)["values"]
        _assert_optional_series_equal(self, actual, expected)

    def test_macd_matches_previous_sma_seeded_implementation(self) -> None:
        closes = [100.0 + index * 0.55 + math.sin(index / 3.0) for index in range(90)]
        fast = _legacy_ema(closes, 12)
        slow = _legacy_ema(closes, 26)
        expected_line = [
            a - b if a is not None and b is not None else None
            for a, b in zip(fast, slow)
        ]
        first = next(index for index, value in enumerate(expected_line) if value is not None)
        expected_signal_tail = _legacy_ema(
            [float(value) for value in expected_line[first:]], 9
        )
        expected_signal = [None] * first + expected_signal_tail
        expected_histogram = [
            line - signal if line is not None and signal is not None else None
            for line, signal in zip(expected_line, expected_signal)
        ]
        actual = calculate_macd(closes)
        _assert_optional_series_equal(self, actual["line"], expected_line)
        _assert_optional_series_equal(self, actual["signal"], expected_signal)
        _assert_optional_series_equal(self, actual["histogram"], expected_histogram)


if __name__ == "__main__":
    unittest.main()
