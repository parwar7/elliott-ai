from __future__ import annotations

import io
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from elliott_ai.cli import build_parser, main
from elliott_ai.experience import pattern_dna_content_hash
from elliott_ai.experience_analogue import (
    ANALOGUE_COMPARISON_CALCULATION_VERSION,
    ANALOGUE_COMPARISON_SCHEMA_VERSION,
    COMPARISON_METHODS,
    COMPARISON_SPECIFICATION_VERSION,
    DIMENSION_BY_ID,
    DIMENSION_DEFINITIONS,
    DIMENSION_STATES,
    analogue_comparison_content_hash,
    compare_dimension_values,
    compare_endpoint_pair,
    compare_filtered_experiences,
    comparison_specification,
    render_analogue_comparisons,
)
from elliott_ai.experience_comparison import (
    filter_structurally_comparable_cases,
)
from elliott_ai.knowledge import KnowledgeStore
from scripts.test_experience_comparison import _current, _historical
from scripts.test_experience_engine import _persist_phase4_fixture


def _rehash(case: dict[str, object]) -> dict[str, object]:
    dna = case["endpoint_dna"]
    assert isinstance(dna, dict)
    dna.pop("content_hash", None)
    dna.pop("dna_id", None)
    preliminary = pattern_dna_content_hash(dna)
    dna["dna_id"] = f"pattern_dna_{preliminary[:32]}"
    dna["content_hash"] = pattern_dna_content_hash(dna)
    return case


def _rich(case: dict[str, object], *, offset: float = 0.0) -> dict[str, object]:
    dna = case["endpoint_dna"]
    assert isinstance(dna, dict)
    groups = dna["groups"]
    assert isinstance(groups, dict)
    source = groups["source_identity"]["values"]["source"]
    source.update(
        {
            "symbol": "NASDAQ:TEST",
            "volume_scope": "consolidated",
            "session": "regular",
        }
    )
    price = groups["price_shape"]["values"]
    price.update(
        {
            "start_price": 100.0,
            "end_price": 120.0 + offset,
            "signed_change": 20.0 + offset,
            "absolute_change": 20.0 + offset,
            "percentage_change": 20.0 + offset,
            "log_return": 0.1823215568 + offset / 100.0,
            "normalized_range_pct": 24.0 + offset,
            "directional_efficiency": 0.75 + offset / 100.0,
            "direction": "up",
            "acceleration": {"state": "stable"},
        }
    )
    groups["duration"].update(
        {
            "status": "available",
            "values": {
                "candles": 20.0 + offset,
                "clock_seconds": (20.0 + offset) * 86_400,
                "clock_days": 20.0 + offset,
            },
        }
    )
    groups["pivot_path"].update(
        {
            "status": "available",
            "values": {
                "pivot_count": 5.0,
                "start_alignment": {"status": "aligned"},
                "end_alignment": {"status": "aligned"},
            },
        }
    )
    groups["fibonacci_relationships"].update(
        {
            "status": "available",
            "values": {
                "retracement": {"status": "available", "value": 0.618},
                "extension": {"status": "available", "value": 1.618},
                "overlap": {"status": "available", "value": 0.10},
            },
        }
    )
    groups["channel_behaviour"].update(
        {
            "status": "available",
            "values": {
                "fit_r_squared": 0.90,
                "terminal_deviation_sigma": 0.40,
                "terminal_break_status": "inside_channel",
            },
        }
    )
    groups["momentum"].update(
        {
            "status": "available",
            "values": {
                "ewo": {
                    "absolute_peak": 8.0 + offset,
                    "absolute_peak_timing": 0.55,
                    "zero_line_crossings": 1.0,
                },
                "macd": {
                    "absolute_peak": 3.0 + offset,
                    "absolute_peak_timing": 0.60,
                    "histogram_state": "stable",
                },
            },
        }
    )
    groups["volume"].update(
        {
            "status": "available",
            "values": {
                "average_ratio_to_baseline": 1.10 + offset / 100.0,
                "terminal_volume_ratio_to_wave_median": 1.20,
                "peak_volume_location": 0.70,
                "effort_vs_result": 0.08,
                "proportion_above_baseline": 0.60,
                "volume_quarantine_status": "clear",
            },
        }
    )
    groups["optional_rsi"].update(
        {
            "status": "available",
            "values": {
                "end_value": 60.0 + offset,
                "slope_per_bar": 0.50,
                "aligned_divergence_candidate": "none",
                "proportion_above_50": 0.70,
            },
        }
    )
    groups["volatility"].update(
        {
            "status": "available",
            "values": {
                "volatility_state": "stable",
                "atr_pct_mean": 2.0,
                "bollinger_outer_pierces": 1.0,
                "keltner_outer_pierces": 2.0,
                "bollinger_terminal_position": 0.25,
            },
        }
    )
    groups["market_context"]["values"].update(
        {
            "session": "regular",
            "broader_regime": "trending",
        }
    )
    groups["structural_context"]["values"].update(
        {
            "parent_wave_id": "parent-wave",
            "internal_child_count": 5.0,
            "internal_structure_family": "impulse",
            "child_structure_status": "valid",
            "completion_status_at_cutoff": "completed",
        }
    )
    return _rehash(case)


def _pair(
    current: dict[str, object] | None = None,
    historical: dict[str, object] | None = None,
    *,
    config: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    current = current or _rich(_current())
    historical = historical or _rich(_historical())
    filtered = filter_structurally_comparable_cases(
        current, [historical], config=config
    )
    return current, historical, filtered


def _comparison(
    current: dict[str, object] | None = None,
    historical: dict[str, object] | None = None,
    *,
    config: dict[str, object] | None = None,
) -> dict[str, object]:
    current, historical, filtered = _pair(current, historical, config=config)
    return compare_filtered_experiences(current, [historical], filtered)


def _dimension(result: dict[str, object], dimension_id: str) -> dict[str, object]:
    comparison = result["comparisons"][0]
    return next(
        item
        for item in comparison["dimensions"]
        if item["dimension_id"] == dimension_id
    )


def _keys(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            result.add(str(key))
            result.update(_keys(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_keys(child))
    return result


class SpecificationAndMethodTests(unittest.TestCase):
    def test_specification_is_explicit_versioned_and_hash_stable(self) -> None:
        first = comparison_specification()
        second = comparison_specification()
        self.assertEqual(first, second)
        self.assertEqual(first["specification_version"], COMPARISON_SPECIFICATION_VERSION)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(len(first["dimensions"]), len(DIMENSION_DEFINITIONS))
        self.assertEqual(len(DIMENSION_BY_ID), len(DIMENSION_DEFINITIONS))
        for item in first["dimensions"]:
            self.assertTrue(
                {
                    "dimension_id",
                    "name",
                    "source_field",
                    "data_type",
                    "comparison_method",
                    "normalization_rule",
                    "missing_data_behavior",
                    "incomparability_behavior",
                    "tolerance_rule",
                    "output_format",
                    "distance_producing",
                    "specification_version",
                }.issubset(item)
            )

    def test_unsupported_specification_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported analogue"):
            comparison_specification("experience-analogue-spec-99.0.0")

    def test_every_comparison_method_is_implemented_by_the_spec(self) -> None:
        self.assertEqual(
            {item.comparison_method for item in DIMENSION_DEFINITIONS},
            set(COMPARISON_METHODS),
        )

    def test_numeric_absolute_and_signed_difference(self) -> None:
        result = compare_dimension_values(
            DIMENSION_BY_ID["percentage_change"], 25.0, 20.0
        )
        self.assertEqual(result["state"], "comparable")
        self.assertEqual(result["difference"]["signed_difference"], 5.0)
        self.assertEqual(result["difference"]["absolute_difference"], 5.0)

    def test_ratio_difference(self) -> None:
        result = compare_dimension_values(
            DIMENSION_BY_ID["duration_candles"], 30.0, 20.0
        )
        self.assertEqual(result["difference"]["ratio"], 1.5)
        self.assertEqual(result["difference"]["absolute_ratio_delta"], 0.5)

    def test_ratio_with_zero_reference_is_not_fabricated(self) -> None:
        result = compare_dimension_values(
            DIMENSION_BY_ID["duration_candles"], 30.0, 0.0
        )
        self.assertEqual(result["state"], "unsupported_by_spec")
        self.assertIsNone(result["difference"]["ratio"])

    def test_categorical_exact_and_descriptive_methods(self) -> None:
        exact = compare_dimension_values(DIMENSION_BY_ID["direction"], "up", "UP")
        descriptive = compare_dimension_values(
            DIMENSION_BY_ID["provider"], "TradingView", "Polygon"
        )
        self.assertTrue(exact["difference"]["exact_match"])
        self.assertEqual(descriptive["difference"]["relationship"], "different")

    def test_categorical_compatibility_uses_versioned_matrix(self) -> None:
        result = compare_dimension_values(
            DIMENSION_BY_ID["structural_role_class"], "zigzag.c", "flat.c"
        )
        self.assertEqual(result["state"], "comparable")
        self.assertEqual(result["difference"]["compatibility_class"], "compatible")
        self.assertEqual(result["difference"]["rule_id"], "ROLE-TERMINAL-C")

    def test_ordered_state_distance(self) -> None:
        result = compare_dimension_values(
            DIMENSION_BY_ID["volatility_state"], "expanding", "contracting"
        )
        self.assertEqual(result["difference"]["signed_order_distance"], 2)
        self.assertEqual(result["difference"]["absolute_order_distance"], 2)

    def test_tolerance_boundary_is_inclusive(self) -> None:
        inside = compare_dimension_values(
            DIMENSION_BY_ID["percentage_change"], 25.0, 20.0
        )
        outside = compare_dimension_values(
            DIMENSION_BY_ID["percentage_change"], 25.000001, 20.0
        )
        self.assertEqual(
            inside["tolerance_band_result"]["status"], "within_tolerance"
        )
        self.assertEqual(
            outside["tolerance_band_result"]["status"], "outside_tolerance"
        )

    def test_invalid_numeric_values_are_unsupported_not_zero(self) -> None:
        for value in (math.nan, math.inf, "20", True):
            with self.subTest(value=value):
                result = compare_dimension_values(
                    DIMENSION_BY_ID["percentage_change"], value, 20.0
                )
                self.assertEqual(result["state"], "unsupported_by_spec")
                self.assertIsNone(result["difference"])


class StateAndLevelTests(unittest.TestCase):
    def test_all_required_dimension_states_are_reachable(self) -> None:
        observed = {"comparable"}
        current = _rich(_current())
        historical = _rich(_historical())

        current["endpoint_dna"]["groups"]["optional_rsi"]["values"]["end_value"] = None
        _rehash(current)
        observed.add(_dimension(_comparison(current, historical), "rsi_end_value")["state"])

        current = _rich(_current())
        historical = _rich(_historical())
        historical["endpoint_dna"]["groups"]["optional_rsi"]["values"]["end_value"] = None
        _rehash(historical)
        observed.add(_dimension(_comparison(current, historical), "rsi_end_value")["state"])

        current = _rich(_current())
        historical = _rich(_historical())
        current["endpoint_dna"]["groups"]["optional_rsi"]["values"]["end_value"] = None
        historical["endpoint_dna"]["groups"]["optional_rsi"]["values"]["end_value"] = None
        _rehash(current)
        _rehash(historical)
        observed.add(_dimension(_comparison(current, historical), "rsi_end_value")["state"])

        cross_market = _rich(
            _historical(
                market_type="crypto",
                venue="BINANCE",
                feed="tv|BINANCE:TEST|daily",
            )
        )
        observed.add(
            _dimension(
                _comparison(
                    _rich(_current()),
                    cross_market,
                    config={"allow_cross_market": True},
                ),
                "timeframe",
            )["state"]
        )

        other_feed = _rich(_historical(feed="other|NASDAQ:TEST|daily"))
        observed.add(
            _dimension(_comparison(_rich(_current()), other_feed), "ewo_absolute_peak")[
                "state"
            ]
        )

        other_timeframe = _rich(_historical(timeframe="4h"))
        other_timeframe["endpoint_dna"]["groups"]["source_identity"]["values"][
            "source"
        ]["feed_identity"] = "tv|NASDAQ:TEST|daily|regular"
        _rehash(other_timeframe)
        observed.add(
            _dimension(
                _comparison(_rich(_current()), other_timeframe), "ewo_zero_crossings"
            )["state"]
        )

        invalid = _rich(_historical())
        invalid["endpoint_dna"]["groups"]["price_shape"]["values"][
            "percentage_change"
        ] = "twenty"
        _rehash(invalid)
        observed.add(
            _dimension(_comparison(_rich(_current()), invalid), "percentage_change")[
                "state"
            ]
        )
        self.assertEqual(observed, set(DIMENSION_STATES))

    def test_level_one_permits_raw_same_degree_momentum(self) -> None:
        result = _comparison()
        self.assertEqual(_dimension(result, "ewo_absolute_peak")["state"], "comparable")

    def test_level_two_omits_raw_momentum_but_keeps_normalized_geometry(self) -> None:
        result = _comparison(
            _rich(_current()), _rich(_historical(degree="Intermediate"))
        )
        self.assertEqual(
            result["comparisons"][0]["comparison_level_result"]["level"], 2
        )
        self.assertEqual(
            _dimension(result, "ewo_absolute_peak")["state"],
            "structurally_inapplicable",
        )
        self.assertEqual(_dimension(result, "percentage_change")["state"], "comparable")

    def test_level_three_compares_compatible_roles(self) -> None:
        result = _comparison(
            _rich(_current("double_three.y")),
            _rich(_historical("triple_three.y")),
        )
        comparison = result["comparisons"][0]
        self.assertEqual(comparison["comparison_level_result"]["level"], 3)
        self.assertEqual(
            _dimension(result, "structural_role_class")["difference"][
                "compatibility_class"
            ],
            "compatible",
        )

    def test_level_four_uses_only_permitted_normalized_dimensions(self) -> None:
        result = _comparison(
            _rich(_current()),
            _rich(
                _historical(
                    market_type="crypto",
                    venue="BINANCE",
                    feed="tv|BINANCE:TEST|daily",
                )
            ),
            config={"allow_cross_market": True},
        )
        self.assertEqual(result["comparisons"][0]["comparison_level_result"]["level"], 4)
        self.assertEqual(_dimension(result, "percentage_change")["state"], "comparable")
        self.assertEqual(
            _dimension(result, "ewo_absolute_peak")["state"],
            "structurally_inapplicable",
        )

    def test_normalized_volume_survives_feed_mismatch_when_scope_matches(self) -> None:
        result = _comparison(
            _rich(_current()), _rich(_historical(feed="other|NASDAQ:TEST|daily"))
        )
        self.assertEqual(
            _dimension(result, "ewo_absolute_peak")["state"], "feed_incomparable"
        )
        self.assertEqual(
            _dimension(result, "volume_average_to_baseline")["state"], "comparable"
        )

    def test_volume_scope_mismatch_is_provenance_incomparable(self) -> None:
        historical = _rich(_historical())
        historical["endpoint_dna"]["groups"]["source_identity"]["values"]["source"][
            "volume_scope"
        ] = "exchange_only"
        _rehash(historical)
        result = _comparison(_rich(_current()), historical)
        self.assertEqual(
            _dimension(result, "volume_average_to_baseline")["state"],
            "provenance_incomparable",
        )


class FilteringHashAndLeakageTests(unittest.TestCase):
    def test_structurally_excluded_case_is_never_compared(self) -> None:
        current = _rich(_current("triangle.b"))
        historical = _rich(_historical("zigzag.b"))
        result = _comparison(current, historical)
        self.assertEqual(result["comparisons"], [])
        self.assertEqual(result["counts"]["phase5a2_excluded_selected"], 1)
        self.assertEqual(
            result["phase5a2_excluded_cases"][0]["rule"],
            "HARD-STRUCTURAL-INCOMPATIBILITY",
        )

    def test_cutoff_leakage_is_rejected_before_comparison(self) -> None:
        historical = _rich(_historical())
        historical["endpoint_dna"]["cutoff"] = "2026-02-01T00:00:00+00:00"
        _rehash(historical)
        result = _comparison(_rich(_current()), historical)
        self.assertEqual(result["comparisons"], [])
        self.assertEqual(
            result["phase5a2_excluded_cases"][0]["rule"],
            "DNA-ENDPOINT-CUTOFF-LEAKAGE",
        )

    def test_outcome_and_confirmation_payloads_cannot_change_comparison(self) -> None:
        current, historical, filtered = _pair()
        baseline = compare_filtered_experiences(current, [historical], filtered)
        attacked_current = deepcopy(current)
        attacked_historical = deepcopy(historical)
        attacked_current["confirmation_dna"] = {"future": "five-away"}
        attacked_current["resolved_outcome_dna"] = {"winner": "anything"}
        attacked_historical["confirmation_dna"] = {"future": "invalidation"}
        attacked_historical["reviewed_outcome"] = {"resolved": True}
        attacked = compare_filtered_experiences(
            attacked_current, [attacked_historical], filtered
        )
        self.assertEqual(baseline, attacked)
        self.assertNotIn("winner", str(attacked))

    def test_repeatability_content_hash_and_source_nonmutation(self) -> None:
        current, historical, filtered = _pair()
        before_current = deepcopy(current)
        before_historical = deepcopy(historical)
        first = compare_filtered_experiences(current, [historical], filtered)
        second = compare_filtered_experiences(current, [historical], filtered)
        self.assertEqual(first, second)
        self.assertEqual(first["content_hash"], analogue_comparison_content_hash(first))
        comparison = first["comparisons"][0]
        self.assertEqual(
            comparison["content_hash"], analogue_comparison_content_hash(comparison)
        )
        self.assertEqual(current, before_current)
        self.assertEqual(historical, before_historical)

    def test_pair_api_rejects_noneligible_filter_item(self) -> None:
        current = _rich(_current())
        historical = _rich(_historical())
        with self.assertRaisesRegex(ValueError, "eligible"):
            compare_endpoint_pair(
                current,
                historical,
                {"eligible": False, "excluded": True, "comparison_level": None},
            )

    def test_empty_one_and_multiple_candidate_pools(self) -> None:
        current = _rich(_current())
        empty_filter = filter_structurally_comparable_cases(current, [])
        empty = compare_filtered_experiences(current, [], empty_filter)
        self.assertEqual(empty["status"], "unavailable_no_eligible_candidates")

        one_history = _rich(_historical(case_id="b-case", episode_id="b-episode"))
        one_filter = filter_structurally_comparable_cases(current, [one_history])
        one = compare_filtered_experiences(current, [one_history], one_filter)
        self.assertEqual(one["counts"]["compared"], 1)

        a_history = _rich(_historical(case_id="a-case", episode_id="a-episode"))
        many_filter = filter_structurally_comparable_cases(
            current, [one_history, a_history]
        )
        many = compare_filtered_experiences(
            current, [one_history, a_history], many_filter
        )
        self.assertEqual(
            [
                item["historical_experience_identifier"]["experience_case_id"]
                for item in many["comparisons"]
            ],
            ["a-case", "b-case"],
        )
        self.assertTrue(
            {
                "rank",
                "rank_position",
                "similarity_score",
                "overall_distance",
                "probability",
            }.isdisjoint(_keys(many))
        )

    def test_candidate_and_maximum_level_selection(self) -> None:
        current = _rich(_current())
        same = _rich(_historical(case_id="same", episode_id="same-episode"))
        adjacent = _rich(
            _historical(
                degree="Intermediate", case_id="adjacent", episode_id="adjacent-episode"
            )
        )
        filtered = filter_structurally_comparable_cases(current, [same, adjacent])
        selected = compare_filtered_experiences(
            current,
            [same, adjacent],
            filtered,
            candidate_case_ids=["same", "adjacent"],
            maximum_comparison_level=1,
        )
        self.assertEqual(selected["counts"]["compared"], 1)
        self.assertEqual(
            selected["eligible_but_omitted_by_requested_level"][0][
                "experience_case_id"
            ],
            "adjacent",
        )

    def test_phase5a2_rejects_explicit_immutable_reference_failure(self) -> None:
        historical = _rich(_historical())
        historical["source_reference_integrity"] = {
            "valid": False,
            "checks": [{"check": "source_fingerprint_hash", "passed": False}],
        }
        filtered = filter_structurally_comparable_cases(
            _rich(_current()), [historical]
        )
        self.assertEqual(
            filtered["excluded"][0]["rule"], "HARD-INVALID-IMMUTABLE-REFERENCE"
        )


class StoreCliAndRenderingTests(unittest.TestCase):
    def test_store_validates_real_immutable_sources_and_cold_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "experience.sqlite3")
            fixture = _persist_phase4_fixture(store)
            created = store.create_experience_case(str(fixture["case"]["case_id"]))
            case_id = str(created["experience_case_id"])
            record = store._experience_comparison_record(case_id)
            self.assertTrue(record["source_reference_integrity"]["valid"])
            result = store.compare_experience_cases(case_id)
            self.assertEqual(result["status"], "unavailable_no_eligible_candidates")
            self.assertEqual(result["counts"]["compared"], 0)

    def test_cli_parser_exposes_requested_phase5a3_controls(self) -> None:
        args = build_parser().parse_args(
            [
                "experience",
                "compare",
                "current-case",
                "--candidate",
                "history-case",
                "--level",
                "3",
                "--spec-version",
                COMPARISON_SPECIFICATION_VERSION,
                "--format",
                "text",
                "--explain",
                "--show-provenance",
                "--show-hashes",
            ]
        )
        self.assertEqual(args.experience_command, "compare")
        self.assertEqual(args.candidate, ["history-case"])
        self.assertEqual(args.level, 3)
        self.assertEqual(args.format, "text")

    def test_cli_json_output(self) -> None:
        result = _comparison()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.compare_experience_cases.return_value = result
            output = io.StringIO()
            with patch("sys.stdout", output):
                main(["experience", "compare", "current-case", "--format", "json"])
        self.assertIn(ANALOGUE_COMPARISON_SCHEMA_VERSION, output.getvalue())
        self.assertIn('"comparisons"', output.getvalue())

    def test_cli_human_readable_explanation_output(self) -> None:
        result = _comparison()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.compare_experience_cases.return_value = result
            output = io.StringIO()
            with patch("sys.stdout", output):
                main(
                    [
                        "experience",
                        "compare",
                        "current-case",
                        "--format",
                        "text",
                        "--explain",
                        "--show-provenance",
                        "--show-hashes",
                    ]
                )
        rendered = output.getvalue()
        self.assertIn("# Experience Analogue Comparison", rendered)
        self.assertIn("| Dimension | State |", rendered)
        self.assertIn("Provenance:", rendered)
        self.assertIn("Comparison hash:", rendered)
        self.assertIn("no best-analogue rank", rendered)

    def test_renderer_handles_empty_pool(self) -> None:
        current = _rich(_current())
        filtered = filter_structurally_comparable_cases(current, [])
        result = compare_filtered_experiences(current, [], filtered)
        rendered = render_analogue_comparisons(result)
        self.assertIn("No Phase 5A.2-eligible", rendered)

    def test_schema_and_calculation_versions_are_exposed(self) -> None:
        result = _comparison()
        self.assertEqual(result["schema_version"], ANALOGUE_COMPARISON_SCHEMA_VERSION)
        self.assertEqual(
            result["calculation_version"], ANALOGUE_COMPARISON_CALCULATION_VERSION
        )
        self.assertEqual(
            result["comparison_specification_version"],
            COMPARISON_SPECIFICATION_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
