from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elliott_ai.agent import AnalysisRequest, ElliottAgent, _anchor_datetime
from elliott_ai.degrees import validate_degree_hierarchy_rules
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.market_data import (
    build_zoom_windows,
    parse_number,
    reconcile_timeframes,
    summarize_market_file,
)
from elliott_ai.providers import PacketProvider
from elliott_ai.reporting import render_degree_report
from elliott_ai.review import build_correction_template, validate_review
from elliott_ai.schema import validate_analysis_shape, validate_degree_resolution_shape


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _candles(count: int = 45) -> list[dict[str, object]]:
    rows = []
    for index in range(count):
        price = 10.0 + index * 0.4 + (1.5 if index % 7 == 0 else 0.0)
        rows.append(
            {
                "date": f"2026-01-{index + 1:02d}" if index < 31 else f"2026-02-{index - 30:02d}",
                "open": price - 0.2,
                "high": price + 0.5,
                "low": price - 0.7,
                "close": price,
                "volume": "1.25 M",
            }
        )
    return rows


def _wave_node(
    wave_id: str,
    parent_wave_id: str | None,
    degree: str,
    wave: str,
    position: str,
    start_date: str,
    start_price: float,
    end_date: str,
    end_price: float,
    *,
    structure: str,
    children: list[str] | None = None,
    terminal: bool = True,
) -> dict[str, object]:
    child_ids = children or []
    return {
        "wave_id": wave_id,
        "parent_wave_id": parent_wave_id,
        "degree": degree,
        "degree_status": "confirmed",
        "degree_confidence": 0.85,
        "wave": wave,
        "sequence_position": position,
        "timeframe": "daily",
        "direction": "up" if position in {"1", "3", "5"} or parent_wave_id is None else "down",
        "start": {"date": start_date, "price": start_price},
        "end": {"date": end_date, "price": end_price},
        "structure": structure,
        "completion_status": "completed",
        "child_structure_status": "verified" if child_ids else "not_required",
        "terminal_at_available_resolution": terminal,
        "child_wave_ids": child_ids,
        "evidence": ["M1"],
        "confirmations": ["Price anchors are present."],
        "concerns": [],
        "invalidation": None,
    }


def _degree_response(run_id: int, symbol: str = "TEST") -> dict[str, object]:
    child_ids = [f"I{number}" for number in range(1, 6)]
    nodes = [
        _wave_node(
            "P1",
            None,
            "Primary",
            "1",
            "1",
            "2026-01-01",
            10.0,
            "2026-02-14",
            30.0,
            structure="Impulse",
            children=child_ids,
            terminal=False,
        ),
        _wave_node("I1", "P1", "Intermediate", "(1)", "1", "2026-01-01", 10.0, "2026-01-05", 15.0, structure="Impulse"),
        _wave_node("I2", "P1", "Intermediate", "(2)", "2", "2026-01-05", 15.0, "2026-01-08", 12.0, structure="Zigzag"),
        _wave_node("I3", "P1", "Intermediate", "(3)", "3", "2026-01-08", 12.0, "2026-01-20", 25.0, structure="Impulse"),
        _wave_node("I4", "P1", "Intermediate", "(4)", "4", "2026-01-20", 25.0, "2026-01-25", 18.0, structure="Zigzag"),
        _wave_node("I5", "P1", "Intermediate", "(5)", "5", "2026-01-25", 18.0, "2026-02-14", 30.0, structure="Impulse"),
    ]
    return {
        "resolution_status": "final",
        "symbol": symbol,
        "source_run_id": run_id,
        "data_cutoff": "2026-02-14",
        "scale_mode": "logarithmic",
        "scope": "Validated fixture hierarchy.",
        "degree_hierarchy": nodes,
        "active_position": {
            "summary": "Primary 1 completed.",
            "wave_ids": ["P1"],
            "confirmation": "Five children are present.",
            "invalidation": "Below 10.00.",
        },
        "alternate_counts": [],
        "invalidation_levels": ["10.00"],
        "unresolved_items": [],
        "confidence": {"structure": 0.85, "degree": 0.8, "active_wave": 0.8},
        "citations": [{"evidence_id": "M1", "claim": "Daily anchors."}],
        "risk_note": "Research only.",
    }


class _ReadOnlyRunStore:
    def __init__(self, run: dict[str, object]) -> None:
        self.run = run

    def get_run(self, run_id: int) -> dict[str, object] | None:
        return self.run if run_id == 21 else None


class RefinementAnchorRegressionTests(unittest.TestCase):
    @staticmethod
    def _candles(timestamps: list[str]) -> list[dict[str, object]]:
        return [
            {
                "date": timestamp,
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 100.5 + index,
                "volume": 1_000 + index,
            }
            for index, timestamp in enumerate(timestamps)
        ]

    @staticmethod
    def _analysis_response(anchor: str) -> dict[str, object]:
        return {
            "analysis_status": "provisional",
            "symbol": "TEST",
            "data_cutoff": "2026-08-07T20:00:00+00:00",
            "scope": "Anchor contract fixture.",
            "preferred_count": [
                {
                    "degree": "Intermediate",
                    "wave": "(3)",
                    "start": anchor,
                    "end": "2026-05-18 @ 408.61",
                    "structure": "Candidate motive leg.",
                    "status": "provisional",
                    "evidence": [],
                }
            ],
            "alternate_counts": [],
            "confirmations": [],
            "invalidation_levels": [],
            "missing_data": [],
            "confidence": {"structure": 0.4, "degree": 0.4, "active_wave": 0.4},
            "risk_note": "Fixture.",
            "citations": [],
        }

    def test_anchor_datetime_reads_canonical_and_run21_legacy_forms(self) -> None:
        expected = datetime(2025, 4, 1, tzinfo=timezone.utc)
        self.assertEqual(_anchor_datetime("2025-04-01"), expected)
        self.assertEqual(_anchor_datetime("2025-04-01T00:00:00Z"), expected)
        self.assertEqual(_anchor_datetime("2025-04-01 @ 140.53"), expected)
        self.assertEqual(_anchor_datetime("2025-04-01 140.53"), expected)

    def test_future_analysis_requires_canonical_anchor_and_historical_format_is_readable(self) -> None:
        self.assertEqual(
            validate_analysis_shape(self._analysis_response("2025-04-01 @ 140.53")),
            [],
        )
        errors = validate_analysis_shape(self._analysis_response("2025-04-01 140.53"))
        self.assertTrue(any("YYYY-MM-DD @ price" in error for error in errors))
        self.assertEqual(
            _anchor_datetime("2025-04-01 140.53"),
            datetime(2025, 4, 1, tzinfo=timezone.utc),
        )

    def test_date_only_endpoint_includes_every_endpoint_day_intraday_candle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "GOOGL_1h_closed.json"
            _write_json(
                path,
                self._candles(
                    [
                        "2026-05-15T13:30:00Z",
                        "2026-05-16T13:30:00Z",
                        "2026-05-18T13:30:00Z",
                        "2026-05-18T14:30:00Z",
                        "2026-05-18T19:30:00Z",
                    ]
                ),
            )
            window = build_zoom_windows(
                [path],
                [
                    {
                        "segment_id": "endpoint-inclusive",
                        "start": "2026-05-15",
                        "end": "2026-05-18",
                    }
                ],
            )[0]
        self.assertEqual(window["status"], "prepared")
        self.assertEqual(window["native_bar_count"], 5)
        coverage = window["timeframe_coverage"][0]
        self.assertEqual(coverage["coverage_status"], "full")
        self.assertTrue(coverage["endpoint_day_included"])
        self.assertEqual(coverage["segment_candle_count"], 5)

    def test_run21_style_anchors_create_focused_i3_with_coverage_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            dates = {
                "daily": [
                    "2022-11-01",
                    "2023-06-01",
                    "2024-01-02",
                    "2025-01-31",
                    "2025-02-03",
                    "2025-02-14",
                    "2025-03-14",
                    "2025-03-28",
                    "2025-04-01",
                    "2025-05-01",
                    "2025-08-01",
                    "2025-11-03",
                    "2026-02-02",
                    "2026-04-01",
                    "2026-05-18",
                    "2026-06-01",
                    "2026-06-15",
                    "2026-07-15",
                    "2026-08-07",
                ],
                "4h": [
                    "2022-05-11T13:30:00Z",
                    "2022-11-01T13:30:00Z",
                    "2023-06-01T13:30:00Z",
                    "2024-01-02T13:30:00Z",
                    "2025-01-31T17:30:00Z",
                    "2025-02-03T13:30:00Z",
                    "2025-02-14T13:30:00Z",
                    "2025-03-14T13:30:00Z",
                    "2025-03-28T13:30:00Z",
                    "2025-04-01T13:30:00Z",
                    "2025-05-01T13:30:00Z",
                    "2025-08-01T13:30:00Z",
                    "2025-11-03T13:30:00Z",
                    "2026-02-02T13:30:00Z",
                    "2026-04-01T13:30:00Z",
                    "2026-05-18T17:30:00Z",
                    "2026-06-01T13:30:00Z",
                    "2026-06-15T13:30:00Z",
                    "2026-07-15T13:30:00Z",
                    "2026-08-07T17:30:00Z",
                ],
                "1h": [
                    "2024-11-18T14:30:00Z",
                    "2025-01-31T20:30:00Z",
                    "2025-02-03T14:30:00Z",
                    "2025-02-14T14:30:00Z",
                    "2025-03-14T14:30:00Z",
                    "2025-03-28T14:30:00Z",
                    "2025-04-01T13:30:00Z",
                    "2025-05-01T13:30:00Z",
                    "2025-08-01T13:30:00Z",
                    "2025-11-03T13:30:00Z",
                    "2026-02-02T13:30:00Z",
                    "2026-04-01T13:30:00Z",
                    "2026-05-18T19:30:00Z",
                    "2026-06-01T13:30:00Z",
                    "2026-06-15T13:30:00Z",
                    "2026-07-15T13:30:00Z",
                    "2026-08-07T19:30:00Z",
                ],
                "15m": [
                    "2026-02-24T14:30:00Z",
                    "2026-03-01T14:30:00Z",
                    "2026-04-01T14:30:00Z",
                    "2026-05-01T14:30:00Z",
                    "2026-05-18T19:45:00Z",
                    "2026-06-01T14:30:00Z",
                    "2026-06-15T14:30:00Z",
                    "2026-07-15T14:30:00Z",
                    "2026-08-07T19:45:00Z",
                ],
            }
            paths = []
            for timeframe, timestamps in dates.items():
                path = workspace / f"GOOGL_{timeframe}_closed.json"
                _write_json(path, self._candles(timestamps))
                paths.append(path.name)
            parent_run = {
                "symbol": "NASDAQ:GOOGL",
                "request": {
                    "market_data_paths": paths,
                    "context_paths": [],
                    "features": [],
                    "blind": True,
                },
                "evidence": {
                    "market_data": [
                        {"timeframe": "daily", "start_date": "2022-11-01"}
                    ]
                },
                "response": {
                    "data_cutoff": "2026-08-07T20:00:00+00:00",
                    "preferred_count": [
                        {
                            "degree": "Intermediate",
                            "wave": "(1)",
                            "start": "2022-11-01 83.34",
                            "end": "2025-02-01 207.05",
                            "structure": "Candidate motive leg.",
                            "status": "provisional",
                        },
                        {
                            "degree": "Intermediate",
                            "wave": "(2)",
                            "start": "2025-02-01 207.05",
                            "end": "2025-04-01 140.53",
                            "structure": "Candidate correction.",
                            "status": "provisional",
                        },
                        {
                            "degree": "Intermediate",
                            "wave": "(3)",
                            "start": "2025-04-01 140.53",
                            "end": "2026-05-18 408.61",
                            "structure": "Candidate motive leg.",
                            "status": "provisional",
                        },
                        {
                            "degree": "Intermediate",
                            "wave": "(4)",
                            "start": "2026-05-18 408.61",
                            "end": None,
                            "structure": "Active correction.",
                            "status": "provisional",
                        },
                    ],
                    "scope": "Run-21-style fixture.",
                },
            }
            agent = ElliottAgent(
                workspace=workspace,
                store=_ReadOnlyRunStore(parent_run),
                provider=PacketProvider(),
            )
            packet = agent.build_refinement_packet(21)

        focused_segments = {
            item["segment"]["parent_wave"]: item
            for item in packet["zoom_windows"]
            if item["segment"].get("purpose") == "parent_child_validation"
        }
        self.assertTrue({"(1)", "(2)", "(3)", "(4)"} <= set(focused_segments))
        i3_window = focused_segments["(3)"]
        self.assertEqual(i3_window["status"], "prepared")
        self.assertTrue(i3_window["segment"]["end_inclusive_trading_day"])
        coverage = {
            item["timeframe"]: item for item in i3_window["timeframe_coverage"]
        }
        self.assertEqual(coverage["daily"]["coverage_status"], "full")
        self.assertEqual(coverage["4h"]["coverage_status"], "full")
        self.assertEqual(coverage["1h"]["coverage_status"], "full")
        self.assertEqual(coverage["15m"]["coverage_status"], "partial_before_start")
        self.assertEqual(coverage["4h"]["segment_candle_count"], 7)
        self.assertEqual(coverage["1h"]["segment_candle_count"], 7)
        packet_i3 = next(
            item
            for item in packet["timeframe_coverage"]
            if item["segment"].get("parent_wave") == "(3)"
        )
        self.assertEqual(packet_i3["coverage"], i3_window["timeframe_coverage"])


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        _write_json(
            self.workspace / "AI_BRAIN_MASTER_RULES.json",
            {
                "Wave_3": {
                    "rule": "Wave 3 cannot be shortest; compare price before EWO and volume."
                },
                "Top_Down": "Child waves must remain inside parent boundaries.",
            },
        )
        (self.workspace / "AI_BRAIN_CURRENT.md").write_text(
            "# Current Brain\nUse price structure before indicators.\n", encoding="utf-8"
        )
        _write_json(
            self.workspace / "test_canonical_recount.json",
            {
                "symbol": "TEST",
                "status": "canonical_preferred_count",
                "verdict": "Five waves followed by an ABC correction.",
            },
        )
        _write_json(
            self.workspace / "old_analysis.json",
            {
                "symbol": "TEST",
                "status": "superseded",
                "verdict": "Obsolete special phrase.",
            },
        )
        _write_json(self.workspace / "tv_test_daily.json", _candles())
        self.store = KnowledgeStore(self.workspace / ".elliott_ai" / "test.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_excludes_raw_market_data_and_searches_rules(self) -> None:
        stats = self.store.index_workspace(self.workspace)
        self.assertEqual(stats["documents"], 4)
        hits = self.store.search("Wave 3 EWO volume")
        self.assertTrue(hits)
        self.assertEqual(hits[0].kind, "rulebook")
        self.assertFalse(any("tv_test_daily" in hit.source for hit in hits))

    def test_superseded_reports_are_excluded_by_default(self) -> None:
        self.store.index_workspace(self.workspace)
        self.assertEqual(self.store.search("Obsolete special phrase"), [])
        hits = self.store.search("Obsolete special phrase", include_superseded=True)
        self.assertTrue(any(hit.status == "superseded" for hit in hits))

    def test_packet_run_requires_final_degree_resolution_before_memory(self) -> None:
        self.store.index_workspace(self.workspace)
        agent = ElliottAgent(
            workspace=self.workspace,
            store=self.store,
            provider=PacketProvider(),
        )
        result = agent.analyze(
            AnalysisRequest(
                question="Audit TEST from the highest degree.",
                symbol="TEST",
            )
        )
        self.assertEqual(result["analysis"]["analysis_status"], "evidence_packet_only")
        self.assertTrue(
            any(
                "captured TradingView snapshot" in item
                for item in result["analysis"]["missing_data"]
            )
        )
        self.assertEqual(result["validation_errors"], [])
        self.assertEqual(self.store.stats()["accepted_cases"], 0)

        refinement = agent.build_refinement_packet(result["run_id"])
        self.assertEqual(refinement["request"]["mode"], "recursive_refinement")
        self.assertEqual(len(refinement["zoom_windows"]), 1)
        self.assertEqual(refinement["zoom_windows"][0]["status"], "prepared")
        self.assertEqual(refinement["zoom_windows"][0]["evidence_id"], "Z1")

        degree_packet = agent.resolve_degrees(result["run_id"])
        self.assertEqual(
            degree_packet["degree_resolution"]["resolution_status"],
            "evidence_packet_only",
        )
        self.assertFalse(degree_packet["readiness"]["final_report_ready"])
        with self.assertRaises(ValueError):
            self.store.accept_run(
                result["run_id"],
                title="Unsafe TEST case",
                user_note="Must remain blocked.",
            )

        final_response = _degree_response(result["run_id"])
        final_resolution_id = self.store.save_degree_resolution(
            run_id=result["run_id"],
            provider="test",
            model="fixture",
            request={"mode": "degree_resolution"},
            response=final_response,
            validation_errors=[],
            readiness={
                "final_report_ready": True,
                "blockers": [],
                "warnings": [],
                "deterministic_impulse_verification": [],
            },
        )
        self.store.accept_run(
            result["run_id"],
            title="Reviewed TEST canonical case",
            user_note="Manually checked.",
            resolution_id=final_resolution_id,
        )
        self.store.index_workspace(self.workspace)
        self.assertEqual(self.store.stats()["accepted_cases"], 1)
        hits = self.store.search("Reviewed TEST canonical case")
        self.assertTrue(any(hit.kind == "approved_case_memory" for hit in hits))

    def test_corrections_are_versioned_and_reviewed_status_requires_content(self) -> None:
        self.store.index_workspace(self.workspace)
        agent = ElliottAgent(
            workspace=self.workspace,
            store=self.store,
            provider=PacketProvider(),
        )
        result = agent.analyze(AnalysisRequest(question="Audit TEST.", symbol="TEST"))
        run = self.store.get_run(result["run_id"])
        self.assertIsNotNone(run)
        template = build_correction_template(run)
        self.assertEqual(validate_review(template), [])
        first = self.store.save_review(result["run_id"], template)
        self.assertEqual(first["revision"], 1)

        reviewed = dict(template)
        reviewed["review_status"] = "reviewed"
        reviewed["summary"] = "The original Primary label was rejected after child review."
        reviewed["wave_corrections"] = [
            {
                "action": "reject",
                "target": "Original Primary 1",
                "reason": "Only three lower-timeframe legs were present.",
            }
        ]
        second = self.store.save_review(result["run_id"], reviewed)
        self.assertEqual(second["revision"], 2)
        latest = self.store.get_latest_review(result["run_id"])
        self.assertEqual(latest["review_status"], "reviewed")
        self.assertEqual(self.store.stats()["reviews"], 2)

    def test_blind_packet_excludes_prior_symbol_counts(self) -> None:
        self.store.index_workspace(self.workspace)
        agent = ElliottAgent(
            workspace=self.workspace,
            store=self.store,
            provider=PacketProvider(),
        )
        packet = agent.build_evidence_packet(
            AnalysisRequest(
                question="Blind recount TEST from price.",
                symbol="TEST",
                blind=True,
            )
        )
        self.assertEqual(
            packet["evidence_policy"]["analysis_mode"],
            "strict_blind_rules_only",
        )
        self.assertTrue(packet["knowledge"])
        self.assertTrue(
            all(
                item["kind"] in {"rulebook", "brain_summary"}
                for item in packet["knowledge"]
            )
        )
        self.assertFalse(
            any("test_canonical_recount" in item["source"] for item in packet["knowledge"])
        )


class DegreeResolutionTests(unittest.TestCase):
    def test_standard_degree_hierarchy_and_impulse_price_rules_pass(self) -> None:
        response = _degree_response(7)
        self.assertEqual(
            validate_degree_resolution_shape(response, source_run_id=7), []
        )
        self.assertEqual(validate_degree_hierarchy_rules(response), [])

    def test_vague_degree_and_wave_four_overlap_are_rejected(self) -> None:
        response = _degree_response(7)
        response["degree_hierarchy"][0]["degree"] = "Higher degree"
        shape_errors = validate_degree_resolution_shape(response, source_run_id=7)
        self.assertTrue(any("standard Elliott degree" in item for item in shape_errors))

        response = _degree_response(7)
        wave_four = next(
            item for item in response["degree_hierarchy"] if item["wave_id"] == "I4"
        )
        wave_four["end"]["price"] = 14.0
        rule_errors = validate_degree_hierarchy_rules(response)
        self.assertTrue(any("overlaps Wave 1" in item for item in rule_errors))

    def test_clean_report_orders_degrees_and_discloses_provisional_status(self) -> None:
        response = _degree_response(7)
        run = {
            "id": 7,
            "symbol": "TEST",
            "evidence": {
                "data_freshness": {
                    "source_mode": "file_snapshot",
                    "overall_status": "current_snapshot",
                }
            },
        }
        resolution = {
            "id": 3,
            "provider": "test",
            "model": "fixture",
            "response": response,
            "readiness": {
                "final_report_ready": False,
                "reconciliation_status": "passed",
                "completed_wave_count": 6,
                "impulse_sequences_checked": 0,
                "blockers": ["Indicator verification is incomplete."],
                "warnings": [],
                "deterministic_impulse_verification": [],
            },
        }
        report = render_degree_report(run, resolution)
        self.assertIn("PROVISIONAL DEGREE REPORT", report)
        self.assertLess(report.index("## Primary Degree"), report.index("## Intermediate Degree"))
        self.assertIn("Indicator verification is incomplete.", report)


class MarketDataTests(unittest.TestCase):
    def test_volume_suffix_and_ewo_summary(self) -> None:
        self.assertEqual(parse_number("29.96 narrow-space M", allow_suffix=True), 29_960_000)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tv_test_daily.json"
            _write_json(path, _candles())
            summary = summarize_market_file(path)
        self.assertEqual(summary["bar_count"], 45)
        self.assertEqual(summary["timeframe"], "daily")
        self.assertEqual(summary["ewo"]["available_bars"], 11)
        self.assertAlmostEqual(summary["volume"]["cumulative"], 56_250_000)
        self.assertEqual(summary["snapshot"]["source_mode"], "file_snapshot")
        self.assertFalse(summary["snapshot"]["live_stream"])
        views = summary["analysis_views"]
        self.assertEqual(views["scaled_full_history"]["bar_count"], 45)
        self.assertEqual(views["recent_native"]["bar_count"], 45)
        self.assertEqual(len(views["pivot_ladders"]), 3)
        self.assertIn("ewo", views["recent_native"]["columns"])
        self.assertIn("macd_histogram", views["recent_native"]["columns"])
        self.assertEqual(views["recent_native"]["columns"][-1], "keltner_position")
        self.assertEqual(
            len(views["recent_native"]["bars"][-1]),
            len(views["recent_native"]["columns"]),
        )

    def test_cross_timeframe_mismatch_is_quarantined(self) -> None:
        daily = []
        hourly = []
        for day in range(1, 6):
            daily.append(
                {
                    "date": f"2026-07-{day:02d}",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 300.0,
                }
            )
            hourly.extend(
                [
                    {
                        "date": f"2026-07-{day:02d}T14:00:00+00:00",
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.0,
                        "close": 10.5,
                        "volume": 100.0,
                    },
                    {
                        "date": f"2026-07-{day:02d}T15:00:00+00:00",
                        "open": 10.5,
                        "high": 12.0,
                        "low": 10.0 if day < 5 else 8.0,
                        "close": 11.0,
                        "volume": 200.0 if day < 5 else 20.0,
                    },
                ]
            )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            daily_path = root / "tv_test_daily.json"
            hourly_path = root / "tv_test_1h.json"
            _write_json(daily_path, daily)
            _write_json(hourly_path, hourly)
            result = reconcile_timeframes([daily_path, hourly_path])
        self.assertEqual(result["overall_status"], "failed")
        comparison = result["comparisons"][0]
        self.assertTrue(comparison["quarantined"])
        self.assertEqual(comparison["overlap_days"], 5)
        self.assertEqual(comparison["largest_mismatches"][0]["date"], "2026-07-05")

    def test_price_can_pass_while_cross_timeframe_volume_is_quarantined(self) -> None:
        daily = []
        hourly = []
        for day in range(1, 6):
            daily.append(
                {
                    "date": f"2026-07-{day:02d}",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 10_000.0,
                }
            )
            hourly.extend(
                [
                    {
                        "date": f"2026-07-{day:02d}T14:00:00+00:00",
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.0,
                        "close": 10.5,
                        "volume": 100.0,
                    },
                    {
                        "date": f"2026-07-{day:02d}T15:00:00+00:00",
                        "open": 10.5,
                        "high": 12.0,
                        "low": 10.0,
                        "close": 11.0,
                        "volume": 200.0,
                    },
                ]
            )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            daily_path = root / "tv_test_daily.json"
            hourly_path = root / "tv_test_1h.json"
            _write_json(daily_path, daily)
            _write_json(hourly_path, hourly)
            result = reconcile_timeframes([daily_path, hourly_path])
        self.assertEqual(result["overall_status"], "passed_with_volume_limitations")
        comparison = result["comparisons"][0]
        self.assertEqual(comparison["price_status"], "passed")
        self.assertFalse(comparison["price_quarantined"])
        self.assertEqual(
            comparison["volume_status"], "not_comparable_across_timeframes"
        )
        self.assertTrue(comparison["volume_quarantined"])

    def test_zoom_window_selects_and_scales_complete_segment(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        daily = []
        for index in range(200):
            date = start + timedelta(days=index)
            price = 100.0 + index * 0.25
            daily.append(
                {
                    "date": date.isoformat(),
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price + 0.25,
                    "volume": 1_000.0 + index,
                }
            )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tv_test_daily.json"
            _write_json(path, daily)
            result = build_zoom_windows(
                [path],
                [
                    {
                        "segment_id": "S1",
                        "purpose": "full_history_control",
                        "start": daily[0]["date"],
                        "end": daily[-1]["date"],
                    }
                ],
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "prepared")
        self.assertEqual(result[0]["source_timeframe"], "daily")
        self.assertEqual(result[0]["native_bar_count"], 200)
        self.assertEqual(result[0]["view_bar_count"], 120)
        self.assertEqual(result[0]["view_mode"], "consecutive_aggregation_to_120_bars")


if __name__ == "__main__":
    unittest.main()
