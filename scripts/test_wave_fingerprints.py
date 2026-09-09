from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elliott_ai.evidence import (
    EVIDENCE_STATUSES,
    EVIDENCE_TYPES,
    make_evidence_item,
    serialize_evidence_item,
    validate_evidence_item,
)
from elliott_ai.fingerprints import (
    CALCULATION_VERSION,
    FEATURE_SCHEMA_VERSION,
    ROLE_TEMPLATES,
    compare_wave_fingerprints,
    fingerprint_content_hash,
    generate_response_fingerprints,
    generate_wave_fingerprint,
)
from elliott_ai.indicators import enrich_candles, simple_moving_average, with_optional_rsi
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.reporting import render_degree_report
from elliott_ai.wave_metrics import calculate_resolution_analytics
from scripts.test_elliott_ai import _degree_response


def _candles(count: int = 140) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    for index in range(count):
        close = 100.0 + index * 0.28 + math.sin(index / 3.1) * 2.4
        spread = 1.2 + (index % 9) * 0.035
        rows.append(
            {
                "date": (start + timedelta(days=index)).date().isoformat(),
                "open": close - 0.2,
                "high": close + spread,
                "low": close - spread * 0.9,
                "close": close,
                "volume": 900_000.0 + index * 4_000.0 + math.sin(index / 4.0) * 60_000.0,
            }
        )
    return rows


def _metadata(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": "tradingview",
        "exchange": "NASDAQ",
        "market_type": "equity",
        "quote_currency": "USD",
        "resolved_symbol": "NASDAQ:TEST",
        "feed_identity": "tv|NASDAQ|TEST|daily|regular",
        "volume_scope": "consolidated_regular_session",
    }
    result.update(overrides)
    return result


def _wave(
    label: str,
    start_index: int,
    end_index: int,
    *,
    rows: list[dict[str, object]] | None = None,
    family: str = "double-three",
    parent_id: str = "CORR",
    structure: str = "zigzag",
    wave_id: str | None = None,
) -> dict[str, object]:
    data = rows or _candles()
    return {
        "wave_id": wave_id or label,
        "parent_wave_id": parent_id,
        "parent_pattern_family": family,
        "degree": "Minor",
        "sequence_position": label,
        "wave": label,
        "timeframe": "daily",
        "start": {"date": data[start_index]["date"], "price": data[start_index]["close"]},
        "end": {"date": data[end_index]["date"], "price": data[end_index]["close"]},
        "structure": structure,
        "completion_status": "completed",
        "child_structure_status": "not_required",
        "child_wave_ids": [],
    }


def _fingerprint(
    label: str,
    start: int,
    end: int,
    *,
    family: str = "double-three",
    rows: list[dict[str, object]] | None = None,
    metadata: dict[str, object] | None = None,
    features: tuple[str, ...] | None = None,
) -> dict[str, object]:
    data = rows or _candles()
    return generate_wave_fingerprint(
        _wave(label, start, end, rows=data, family=family),
        data,
        source_metadata=metadata or _metadata(),
        cutoff=data[end]["date"],
        features=features,
        symbol="NASDAQ:TEST",
        timeframe="daily",
    )


class FingerprintDeterminismTests(unittest.TestCase):
    def test_deterministic_generation_stable_version_and_content_hash(self) -> None:
        first = _fingerprint("Y", 70, 105)
        second = _fingerprint("Y", 70, 105)
        self.assertEqual(first, second)
        self.assertEqual(first["feature_schema_version"], FEATURE_SCHEMA_VERSION)
        self.assertEqual(first["calculation_version"], CALCULATION_VERSION)
        self.assertEqual(first["content_hash"], fingerprint_content_hash(first))

    def test_different_cutoff_creates_separate_fingerprint(self) -> None:
        rows = _candles()
        wave = _wave("Y", 70, 100, rows=rows)
        first = generate_wave_fingerprint(
            wave,
            rows,
            source_metadata=_metadata(),
            cutoff=rows[100]["date"],
            symbol="NASDAQ:TEST",
            timeframe="daily",
        )
        second = generate_wave_fingerprint(
            wave,
            rows,
            source_metadata=_metadata(),
            cutoff=rows[110]["date"],
            symbol="NASDAQ:TEST",
            timeframe="daily",
        )
        self.assertNotEqual(first["content_hash"], second["content_hash"])
        self.assertNotEqual(first["cutoff"]["timestamp"], second["cutoff"]["timestamp"])

    def test_no_look_ahead_leakage_from_later_candles(self) -> None:
        rows = _candles(125)
        wave = _wave("Y", 60, 95, rows=rows)
        before = generate_wave_fingerprint(
            wave,
            rows,
            source_metadata=_metadata(),
            cutoff=rows[95]["date"],
            symbol="NASDAQ:TEST",
            timeframe="daily",
        )
        extended = deepcopy(rows)
        for index in range(96, len(extended)):
            extended[index]["close"] = 50_000.0 + index
            extended[index]["high"] = 51_000.0 + index
            extended[index]["low"] = 49_000.0 + index
            extended[index]["volume"] = 9_000_000_000.0
        after = generate_wave_fingerprint(
            wave,
            extended,
            source_metadata=_metadata(),
            cutoff=rows[95]["date"],
            symbol="NASDAQ:TEST",
            timeframe="daily",
        )
        self.assertEqual(before, after)

    def test_future_declared_endpoint_is_not_exposed_at_earlier_cutoff(self) -> None:
        rows = _candles()
        fingerprint = generate_wave_fingerprint(
            _wave("Y", 60, 110, rows=rows),
            rows,
            source_metadata=_metadata(),
            cutoff=rows[90]["date"],
            symbol="NASDAQ:TEST",
            timeframe="daily",
        )
        self.assertTrue(fingerprint["cutoff"]["provisional"])
        self.assertEqual(fingerprint["identity"]["end_timestamp"][:10], rows[90]["date"])
        self.assertNotEqual(fingerprint["price"]["end_price"], rows[110]["close"])


class FingerprintIdentityAndTemplateTests(unittest.TestCase):
    def test_wave_a_context_differs_between_zigzag_and_flat(self) -> None:
        zigzag = _fingerprint("A", 55, 80, family="zigzag")
        flat = _fingerprint("A", 55, 80, family="flat")
        self.assertEqual(zigzag["role_template"]["role_id"], "zigzag_a")
        self.assertEqual(flat["role_template"]["role_id"], "flat_a")
        self.assertNotEqual(zigzag["identity_hash"], flat["identity_hash"])

    def test_w_x_y_x2_z_keep_separate_contextual_identities(self) -> None:
        roles = {}
        for label, start, end, family in (
            ("W", 50, 65, "double-three"),
            ("X", 65, 75, "double-three"),
            ("Y", 75, 95, "double-three"),
            ("X2", 95, 105, "triple-three"),
            ("Z", 105, 125, "triple-three"),
        ):
            fingerprint = _fingerprint(label, start, end, family=family)
            roles[label] = fingerprint["role_template"]["role_id"]
            self.assertEqual(fingerprint["identity"]["wave_label"], label)
        self.assertEqual(len(set(roles.values())), 5)
        self.assertEqual(roles["X"], "connector_x")
        self.assertEqual(roles["X2"], "connector_x2")

    def test_all_required_templates_are_soft_and_versioned(self) -> None:
        required = {
            "zigzag:A", "zigzag:B", "zigzag:C", "flat:A", "flat:B", "flat:C",
            "double-three:W", "combination:X", "double-three:Y", "combination:X2",
            "triple-three:Z", "impulse:1", "corrective:A",
        }
        self.assertTrue(required.issubset(ROLE_TEMPLATES))
        for template in ROLE_TEMPLATES.values():
            self.assertEqual(template["hard_rules"], [])
            self.assertTrue(template["template_version"])

    def test_missing_reference_is_unavailable_not_zero(self) -> None:
        fingerprint = _fingerprint("Y", 75, 100)
        ratio = fingerprint["structural"]["extension_ratio"]
        self.assertEqual(ratio["status"], "unavailable")
        self.assertIsNone(ratio["value"])


class IndicatorFingerprintTests(unittest.TestCase):
    def test_rsi_disabled_has_no_block_or_negative_evidence(self) -> None:
        fingerprint = _fingerprint("Y", 70, 105)
        self.assertFalse(fingerprint["identity"]["active_feature_flags"]["rsi"])
        self.assertNotIn("rsi", fingerprint["indicators"])
        self.assertFalse(any(item["evidence_type"] == "rsi" for item in fingerprint["evidence_items"]))

    def test_aligned_rsi_divergence_and_unaligned_rejection(self) -> None:
        reference = _fingerprint("W", 50, 70, features=with_optional_rsi((), enabled=True))
        current = _fingerprint("Y", 75, 105, features=with_optional_rsi((), enabled=True))
        reference["price"]["end_price"] = 100.0
        current["price"]["end_price"] = 110.0
        reference["indicators"]["rsi"]["end_value"] = 70.0
        current["indicators"]["rsi"]["end_value"] = 60.0
        aligned = compare_wave_fingerprints(current, reference, relationship="extension")
        self.assertEqual(aligned["divergence_candidates"]["rsi"]["candidate"], "bearish")
        current["price"]["end_pivot_alignment"]["status"] = "not_aligned"
        unaligned = compare_wave_fingerprints(current, reference, relationship="extension")
        self.assertIsNone(unaligned["divergence_candidates"]["rsi"]["candidate"])
        self.assertEqual(unaligned["divergence_candidates"]["rsi"]["comparability"], "incomparable")

    def test_ewo_and_macd_match_phase_2_enrichment(self) -> None:
        rows = _candles()
        fingerprint = _fingerprint("Y", 55, 100, rows=rows, features=("ewo", "macd"))
        enriched = enrich_candles(rows[:101], features=("ewo", "macd"))
        ewo = [row["ewo"] for row in enriched[55:101] if row["ewo"] is not None]
        macd = [row["macd"] for row in enriched[55:101] if row["macd"] is not None]
        self.assertAlmostEqual(fingerprint["indicators"]["ewo"]["absolute_peak"], max(abs(value) for value in ewo))
        self.assertAlmostEqual(fingerprint["indicators"]["macd"]["maximum"], max(macd))

    def test_volume_normalization_and_effort_result(self) -> None:
        rows = _candles()
        fingerprint = _fingerprint("Y", 55, 100, rows=rows, features=("volume",))
        volumes = [float(row["volume"]) for row in rows[:101]]
        baseline = simple_moving_average(volumes, 50)
        ratios = [volumes[index] / baseline[index] for index in range(55, 101) if baseline[index] is not None]
        block = fingerprint["indicators"]["volume"]
        expected_mean = sum(ratios) / len(ratios)
        self.assertAlmostEqual(block["average_ratio_to_baseline"], expected_mean)
        expected_effort = expected_mean / abs(fingerprint["price"]["percentage_change"])
        self.assertAlmostEqual(block["effort_vs_result"], expected_effort)

    def test_incompatible_volume_feeds_are_not_compared(self) -> None:
        reference = _fingerprint("W", 50, 70, metadata=_metadata(feed_identity="feed-a"))
        current = _fingerprint("Y", 75, 105, metadata=_metadata(feed_identity="feed-b"))
        comparison = compare_wave_fingerprints(current, reference, relationship="extension")
        fields = {item["field"] for item in comparison["incomparable_fields"]}
        self.assertIn("volume_total", fields)
        self.assertNotIn("volume_total", comparison["normalized_ratios"])
        self.assertTrue(any(item["status"] == "incomparable" and item["evidence_type"] == "volume" for item in comparison["evidence_items"]))

    @staticmethod
    def _volatility_rows(expanding: bool) -> list[dict[str, object]]:
        rows = _candles(90)
        for index, row in enumerate(rows):
            if expanding:
                spread = 0.8 if index < 40 else 0.8 + (index - 39) * 0.28
            else:
                spread = 11.0 if index < 40 else max(0.6, 11.0 - (index - 39) * 0.28)
            close = float(row["close"])
            row["high"] = close + spread
            row["low"] = close - spread
        return rows

    def test_atr_expansion_and_contraction(self) -> None:
        expanding_rows = self._volatility_rows(True)
        contracting_rows = self._volatility_rows(False)
        expanding = _fingerprint("Y", 40, 80, rows=expanding_rows, features=("volatility",))
        contracting = _fingerprint("Y", 40, 80, rows=contracting_rows, features=("volatility",))
        self.assertGreater(expanding["indicators"]["volatility"]["atr_expansion_ratio"], 1.0)
        self.assertEqual(expanding["indicators"]["volatility"]["volatility_state"], "expanding")
        self.assertLess(contracting["indicators"]["volatility"]["atr_expansion_ratio"], 1.0)
        self.assertEqual(contracting["indicators"]["volatility"]["volatility_state"], "contracting")


class UnifiedEvidenceTests(unittest.TestCase):
    def test_all_statuses_serialize(self) -> None:
        for status in EVIDENCE_STATUSES - {"invalidating"}:
            with self.subTest(status=status):
                item = make_evidence_item(
                    evidence_type="indicator",
                    feature_name="test.feature",
                    observed_value=1.0,
                    status=status,
                    reason="Deterministic fixture.",
                )
                self.assertEqual(validate_evidence_item(item), [])
                self.assertEqual(json.loads(serialize_evidence_item(item))["status"], status)

    def test_all_required_evidence_types_share_one_contract(self) -> None:
        required = {
            "rsi", "volume", "ewo", "macd", "atr", "fibonacci", "channel", "structural"
        }
        self.assertTrue(required.issubset(EVIDENCE_TYPES))
        for evidence_type in required:
            item = make_evidence_item(
                evidence_type=evidence_type,
                feature_name=f"{evidence_type}.fixture",
                observed_value=None,
                status="unavailable",
                reason="Fixture observation is unavailable.",
            )
            self.assertEqual(validate_evidence_item(item), [])

    def test_indicators_cannot_invalidate_but_structure_can(self) -> None:
        for evidence_type in ("rsi", "volume", "ewo", "macd", "atr", "volatility", "indicator"):
            with self.subTest(evidence_type=evidence_type):
                with self.assertRaises(ValueError):
                    make_evidence_item(
                        evidence_type=evidence_type,
                        feature_name="test",
                        observed_value=True,
                        status="invalidating",
                        reason="Not allowed.",
                        hard_rule=True,
                    )
        structural = make_evidence_item(
            evidence_type="structural_rule",
            feature_name="wave_4_overlap",
            observed_value=True,
            status="invalidating",
            reason="A hard impulse overlap rule was violated.",
            hard_rule=True,
        )
        self.assertEqual(structural["status"], "invalidating")


class FingerprintComparisonTests(unittest.TestCase):
    def test_y_versus_w_comparison(self) -> None:
        reference = _fingerprint("W", 50, 70)
        current = _fingerprint("Y", 75, 105)
        comparison = compare_wave_fingerprints(current, reference, relationship="extension")
        self.assertEqual(comparison["current_wave_id"], "Y")
        self.assertEqual(comparison["reference_wave_id"], "W")
        self.assertIn("price_absolute_change", comparison["normalized_ratios"])
        self.assertNotIn("probability", comparison)

    def test_c_versus_a_comparison(self) -> None:
        reference = _fingerprint("A", 50, 70, family="zigzag")
        current = _fingerprint("C", 75, 105, family="zigzag")
        comparison = compare_wave_fingerprints(current, reference, relationship="extension")
        self.assertEqual(comparison["relationship"], "extension")
        self.assertIn("directional_efficiency", comparison["normalized_ratios"])

    def test_fifth_versus_third_momentum_comparison(self) -> None:
        reference = _fingerprint("3", 45, 75, family="impulse")
        current = _fingerprint("5", 80, 110, family="impulse")
        comparison = compare_wave_fingerprints(current, reference, relationship="extension")
        self.assertIn("ewo_absolute_peak", comparison["normalized_ratios"])
        self.assertIn("macd_absolute_peak", comparison["normalized_ratios"])

    def test_response_engine_pairs_y_with_w(self) -> None:
        rows = _candles()
        parent = _wave("CORR", 45, 105, rows=rows, family="unknown", parent_id="ROOT", structure="double three", wave_id="CORR")
        parent["sequence_position"] = "A"
        parent["child_wave_ids"] = ["W", "X", "Y"]
        nodes = [
            parent,
            _wave("W", 45, 65, rows=rows, wave_id="W"),
            _wave("X", 65, 75, rows=rows, wave_id="X"),
            _wave("Y", 75, 105, rows=rows, wave_id="Y"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tv_test_daily.json"
            path.write_text(json.dumps({"metadata": _metadata(), "data": rows}), encoding="utf-8")
            result = generate_response_fingerprints(
                {"symbol": "NASDAQ:TEST", "data_cutoff": rows[105]["date"], "degree_hierarchy": nodes},
                [path],
                workspace=Path(directory),
            )
        comparison = next(item for item in result["comparisons"] if item["current_wave_id"] == "Y")
        self.assertEqual(comparison["reference_wave_id"], "W")


class FingerprintPersistenceAndReportTests(unittest.TestCase):
    def test_legacy_database_migrates_without_rewriting_old_data_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE legacy_marker(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy_marker(value) VALUES ('keep-me')")
            connection.commit()
            connection.close()
            store = KnowledgeStore(database)
            with store.connect() as migrated:
                self.assertEqual(migrated.execute("SELECT value FROM legacy_marker").fetchone()["value"], "keep-me")
                tables = {row["name"] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"wave_observations", "wave_fingerprints", "feature_schema_versions"}.issubset(tables))
            fingerprint = _fingerprint("Y", 70, 105)
            first = store.save_wave_fingerprint(fingerprint)
            second = store.save_wave_fingerprint(fingerprint)
            self.assertTrue(first["inserted"])
            self.assertFalse(second["inserted"])
            self.assertEqual(first["fingerprint_id"], second["fingerprint_id"])
            self.assertEqual(store.stats()["wave_fingerprints"], 1)

    def test_older_fingerprint_schema_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fingerprint = _fingerprint("Y", 70, 105)
            fingerprint["feature_schema_version"] = "wave-fingerprint-0.9.0"
            fingerprint["identity"]["feature_schema_version"] = "wave-fingerprint-0.9.0"
            fingerprint.pop("content_hash")
            fingerprint["content_hash"] = fingerprint_content_hash(fingerprint)
            saved = store.save_wave_fingerprint(fingerprint)
            loaded = store.get_wave_fingerprint(saved["fingerprint_id"])
            self.assertEqual(loaded["fingerprint"]["feature_schema_version"], "wave-fingerprint-0.9.0")

    def test_report_renders_fingerprints_with_and_without_optional_rsi(self) -> None:
        rows = _candles(100)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "tv_test_daily.json"
            path.write_text(json.dumps({"metadata": _metadata(), "data": rows}), encoding="utf-8")
            response = _degree_response(7, symbol="NASDAQ:TEST")
            base_readiness = {
                "final_report_ready": False,
                "deterministic_impulse_verification": [],
                "blockers": [],
                "warnings": [],
            }
            disabled = calculate_resolution_analytics(
                response, [path], workspace=workspace, features=("scale",), origin_run_id=7
            )
            enabled = calculate_resolution_analytics(
                response,
                [path],
                workspace=workspace,
                features=with_optional_rsi((), enabled=True),
                origin_run_id=7,
            )
            run = {"id": 7, "symbol": "NASDAQ:TEST"}
            disabled_report = render_degree_report(
                run,
                {"id": 1, "response": response, "readiness": {**base_readiness, "analytical_metrics": disabled}},
            )
            enabled_report = render_degree_report(
                run,
                {"id": 2, "response": response, "readiness": {**base_readiness, "analytical_metrics": enabled}},
            )
        self.assertIn("Versioned Wave Fingerprints", disabled_report)
        self.assertIn(FEATURE_SCHEMA_VERSION, disabled_report)
        self.assertNotIn("RSI end", disabled_report)
        self.assertIn("RSI end", enabled_report)


if __name__ == "__main__":
    unittest.main()
