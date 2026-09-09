from __future__ import annotations

import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from elliott_ai.cli import build_parser, main
from elliott_ai.experience_analogue import compare_filtered_experiences
from elliott_ai.experience_comparison import filter_structurally_comparable_cases
from elliott_ai.experience_retrieval import (
    ANALOGUE_RETRIEVAL_CALCULATION_VERSION,
    ANALOGUE_RETRIEVAL_SCHEMA_VERSION,
    DEFAULT_RETRIEVAL_POLICY_ID,
    DEFAULT_RETRIEVAL_POLICY_VERSION,
    GROUP_PRECEDENCE,
    MANDATORY_DIMENSIONS,
    MAXIMUM_RESULT_LIMIT,
    analogue_retrieval_content_hash,
    render_analogue_retrieval,
    render_retrieval_policy,
    retrieval_policy,
    retrieve_analogues,
)
from elliott_ai.knowledge import KnowledgeStore
from scripts.test_experience_analogue import _rehash, _rich
from scripts.test_experience_comparison import _current, _historical
from scripts.test_experience_engine import _persist_phase4_fixture


def _comparison_set(
    current: dict[str, object] | None = None,
    histories: list[dict[str, object]] | None = None,
    *,
    config: dict[str, object] | None = None,
    maximum_level: int = 4,
) -> dict[str, object]:
    current = current or _rich(_current())
    histories = histories if histories is not None else [_rich(_historical())]
    filtered = filter_structurally_comparable_cases(
        current, histories, config=config
    )
    return compare_filtered_experiences(
        current,
        histories,
        filtered,
        maximum_comparison_level=maximum_level,
    )


def _result(
    current: dict[str, object] | None = None,
    histories: list[dict[str, object]] | None = None,
    *,
    config: dict[str, object] | None = None,
    maximum_level: int = 4,
    limit: int = 10,
) -> dict[str, object]:
    comparisons = _comparison_set(
        current, histories, config=config, maximum_level=4
    )
    return retrieve_analogues(
        comparisons,
        maximum_comparison_level=maximum_level,
        limit=limit,
    )


def _ids(result: dict[str, object]) -> list[str]:
    return [
        str(item["experience_case_reference"]["experience_case_id"])
        for item in result["ordered_analogue_references"]
    ]


def _analogue(result: dict[str, object], case_id: str) -> dict[str, object]:
    return next(
        item
        for item in result["ordered_analogue_references"]
        if item["experience_case_reference"]["experience_case_id"] == case_id
    )


def _dimension(analogue: dict[str, object], dimension_id: str) -> dict[str, object]:
    return next(
        item
        for item in analogue["comparison_evidence"]["dimensions"]
        if item["dimension_id"] == dimension_id
    )


def _four_level_fixture() -> tuple[dict[str, object], list[dict[str, object]]]:
    current = _rich(_current("double_three.y"))
    histories = [
        _rich(
            _historical(
                "double_three.y", case_id="level-1", episode_id="episode-1"
            )
        ),
        _rich(
            _historical(
                "double_three.y",
                degree="Intermediate",
                case_id="level-2",
                episode_id="episode-2",
            )
        ),
        _rich(
            _historical(
                "triple_three.y", case_id="level-3", episode_id="episode-3"
            )
        ),
        _rich(
            _historical(
                "double_three.y",
                market_type="crypto",
                venue="BINANCE",
                feed="tv|BINANCE:TEST|daily",
                case_id="level-4",
                episode_id="episode-4",
            )
        ),
    ]
    return current, histories


def _remove_value(
    case: dict[str, object], group: str, *path: str
) -> dict[str, object]:
    current = case["endpoint_dna"]["groups"][group]["values"]
    for part in path[:-1]:
        current = current[part]
    current.pop(path[-1], None)
    return _rehash(case)


class PolicyAndContractTests(unittest.TestCase):
    def test_policy_is_explicit_versioned_and_hash_stable(self) -> None:
        first = retrieval_policy()
        second = retrieval_policy()
        self.assertEqual(first, second)
        self.assertEqual(first["policy_id"], DEFAULT_RETRIEVAL_POLICY_ID)
        self.assertEqual(first["policy_version"], DEFAULT_RETRIEVAL_POLICY_VERSION)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["grouping_precedence"], list(GROUP_PRECEDENCE))
        self.assertEqual(first["mandatory_dimensions"], list(MANDATORY_DIMENSIONS))
        self.assertEqual(
            first["ordering_model"]["final_tie_breaker"]["key"],
            "experience_case_id",
        )
        self.assertFalse(first["diversity_rules"]["enabled"])
        self.assertFalse(first["ordering_model"]["synthetic_overall_score"])

    def test_invalid_policy_id_and_version_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported retrieval policy"):
            retrieval_policy("unknown-policy")
        with self.assertRaisesRegex(ValueError, "policy version"):
            retrieval_policy(policy_version="9.9.9")

    def test_invalid_comparison_level_and_limits_are_rejected(self) -> None:
        comparisons = _comparison_set()
        with self.assertRaisesRegex(ValueError, "maximum_comparison_level"):
            retrieve_analogues(comparisons, maximum_comparison_level=0)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            retrieve_analogues(comparisons, limit=0)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            retrieve_analogues(comparisons, limit=MAXIMUM_RESULT_LIMIT + 1)

    def test_result_contract_versions_hash_and_links_are_present(self) -> None:
        result = _result()
        self.assertEqual(result["schema_version"], ANALOGUE_RETRIEVAL_SCHEMA_VERSION)
        self.assertEqual(
            result["calculation_version"], ANALOGUE_RETRIEVAL_CALCULATION_VERSION
        )
        self.assertEqual(result["content_hash"], analogue_retrieval_content_hash(result))
        analogue = result["ordered_analogue_references"][0]
        self.assertTrue(analogue["experience_pool_gate"]["accepted"])
        self.assertTrue(
            analogue["phase5a2_filter_decision_reference"]["content_hash"]
        )
        self.assertTrue(analogue["phase5a3_comparison_reference"]["content_hash"])
        self.assertTrue(analogue["immutable_input_references"])
        self.assertTrue(analogue["cutoff_verification"]["current"]["valid"])
        self.assertTrue(analogue["cutoff_verification"]["historical"]["valid"])

    def test_tampered_phase5a3_hash_is_rejected(self) -> None:
        comparisons = _comparison_set()
        comparisons["comparisons"][0]["dimensions"][0]["state"] = "missing_both"
        with self.assertRaisesRegex(ValueError, "comparison-set content hash"):
            retrieve_analogues(comparisons)


class TierAndLevelTests(unittest.TestCase):
    def test_empty_accepted_experience_pool_is_valid(self) -> None:
        result = _result(histories=[])
        self.assertEqual(
            result["status"], "unavailable_empty_accepted_experience_pool"
        )
        self.assertEqual(result["ordered_analogue_references"], [])
        self.assertEqual(result["eligible_candidate_count"], 0)
        self.assertIn("No accepted historical", result["availability_explanation"])
        self.assertEqual(result["content_hash"], analogue_retrieval_content_hash(result))

    def test_one_eligible_candidate_is_tier_one(self) -> None:
        result = _result()
        self.assertEqual(result["presented_analogue_count"], 1)
        self.assertEqual(
            result["ordered_analogue_references"][0]["retrieval_tier"]["tier"], 1
        )

    def test_all_four_comparison_levels_and_every_tier(self) -> None:
        current, histories = _four_level_fixture()
        result = _result(
            current, histories, config={"allow_cross_market": True}
        )
        self.assertEqual(_ids(result), ["level-1", "level-2", "level-3", "level-4"])
        levels = [
            item["phase5a2_filter_decision_reference"]["comparison_level"]
            for item in result["ordered_analogue_references"]
        ]
        tiers = [
            item["retrieval_tier"]["tier"]
            for item in result["ordered_analogue_references"]
        ]
        self.assertEqual(levels, [1, 2, 3, 4])
        self.assertEqual(tiers, [1, 2, 2, 3])

    def test_feed_incomparability_prevents_tier_one(self) -> None:
        history = _rich(
            _historical(
                feed="other|NASDAQ:TEST|daily",
                case_id="feed-different",
                episode_id="feed-episode",
            )
        )
        result = _result(histories=[history])
        analogue = result["ordered_analogue_references"][0]
        self.assertEqual(analogue["retrieval_tier"]["tier"], 2)
        self.assertGreater(
            analogue["comparison_summary"]["aggregate"]["incomparable_count"], 0
        )
        self.assertEqual(
            _dimension(analogue, "ewo_absolute_peak")["state"], "feed_incomparable"
        )

    def test_provenance_incomparability_is_explicit(self) -> None:
        history = _rich(
            _historical(
                timeframe="weekly",
                case_id="timeframe-different",
                episode_id="timeframe-episode",
            )
        )
        result = _result(histories=[history])
        analogue = result["ordered_analogue_references"][0]
        self.assertEqual(analogue["retrieval_tier"]["tier"], 2)
        self.assertEqual(
            _dimension(analogue, "duration_candles")["state"],
            "provenance_incomparable",
        )

    def test_mandatory_dimension_absence_moves_case_to_tier_three(self) -> None:
        history = _remove_value(
            _rich(_historical(case_id="partial", episode_id="partial-episode")),
            "pivot_path",
            "start_alignment",
        )
        result = _result(histories=[history])
        analogue = result["ordered_analogue_references"][0]
        self.assertEqual(analogue["retrieval_tier"]["tier"], 3)
        unavailable = analogue["comparison_summary"]["mandatory_dimensions"][
            "unavailable"
        ]
        self.assertEqual(unavailable[0]["dimension_id"], "start_pivot_alignment")

    def test_level_four_structural_inapplicability_remains_visible(self) -> None:
        current, histories = _four_level_fixture()
        result = _result(
            current, histories, config={"allow_cross_market": True}
        )
        analogue = _analogue(result, "level-4")
        self.assertEqual(
            _dimension(analogue, "ewo_absolute_peak")["state"],
            "structurally_inapplicable",
        )
        self.assertGreater(
            analogue["comparison_summary"]["aggregate"]["omitted_count"], 0
        )

    def test_requested_level_marks_higher_levels_unavailable(self) -> None:
        current, histories = _four_level_fixture()
        result = _result(
            current,
            histories,
            config={"allow_cross_market": True},
            maximum_level=2,
        )
        self.assertEqual(_ids(result), ["level-1", "level-2"])
        self.assertEqual(result["unavailable_candidate_count"], 2)
        self.assertEqual(
            {item["experience_case_id"] for item in result["unavailable_candidate_references"]},
            {"level-3", "level-4"},
        )


class OrderingAndMissingDataTests(unittest.TestCase):
    def test_level_precedence_is_lexicographic(self) -> None:
        current = _rich(_current())
        level_one = _rich(
            _historical(case_id="z-level-one", episode_id="z-level-one-episode"),
            offset=30.0,
        )
        level_two = _rich(
            _historical(
                degree="Intermediate",
                case_id="a-level-two",
                episode_id="a-level-two-episode",
            )
        )
        result = _result(current, [level_two, level_one])
        self.assertEqual(_ids(result), ["z-level-one", "a-level-two"])

    def test_grouped_tolerance_and_categorical_counts_order_transparently(self) -> None:
        current = _rich(_current())
        exact = _rich(
            _historical(case_id="z-exact", episode_id="z-exact-episode")
        )
        distant = _rich(
            _historical(case_id="a-distant", episode_id="a-distant-episode"),
            offset=30.0,
        )
        result = _result(current, [distant, exact])
        self.assertEqual(_ids(result), ["z-exact", "a-distant"])
        first = result["ordered_analogue_references"][0]
        second = result["ordered_analogue_references"][1]
        self.assertGreaterEqual(
            first["comparison_summary"]["aggregate"]["within_tolerance_count"],
            second["comparison_summary"]["aggregate"]["within_tolerance_count"],
        )
        self.assertIn(
            "first_differing_key", first["ordering_explanation"]["ordered_before"]
        )

    def test_exact_categorical_match_orders_before_mismatch(self) -> None:
        current = _rich(_current())
        exact = _rich(
            _historical(case_id="z-exact", episode_id="z-exact-episode")
        )
        mismatch = _rich(
            _historical(case_id="a-mismatch", episode_id="a-mismatch-episode")
        )
        mismatch["endpoint_dna"]["groups"]["market_context"]["values"][
            "broader_regime"
        ] = "ranging"
        _rehash(mismatch)
        result = _result(current, [mismatch, exact])
        self.assertEqual(_ids(result), ["z-exact", "a-mismatch"])

    def test_stable_case_id_is_final_tie_breaker(self) -> None:
        current = _rich(_current())
        b_case = _rich(_historical(case_id="b-case", episode_id="b-episode"))
        a_case = _rich(_historical(case_id="a-case", episode_id="a-episode"))
        result = _result(current, [b_case, a_case])
        self.assertEqual(_ids(result), ["a-case", "b-case"])
        difference = result["ordered_analogue_references"][0][
            "ordering_explanation"
        ]["ordered_before"]["first_differing_key"]
        self.assertEqual(difference["key"], "experience_case_id")

    def test_missing_historical_never_improves_order(self) -> None:
        current = _rich(_current())
        complete = _rich(
            _historical(case_id="z-complete", episode_id="z-complete-episode")
        )
        missing = _remove_value(
            _rich(_historical(case_id="a-missing", episode_id="a-missing-episode")),
            "optional_rsi",
            "end_value",
        )
        result = _result(current, [missing, complete])
        self.assertEqual(_ids(result), ["z-complete", "a-missing"])
        self.assertEqual(
            _dimension(_analogue(result, "a-missing"), "rsi_end_value")["state"],
            "missing_historical",
        )

    def test_missing_current_and_missing_both_are_explicit(self) -> None:
        current = _remove_value(_rich(_current()), "optional_rsi", "end_value")
        history_present = _rich(
            _historical(case_id="present", episode_id="present-episode")
        )
        history_missing = _remove_value(
            _rich(_historical(case_id="missing", episode_id="missing-episode")),
            "optional_rsi",
            "end_value",
        )
        result = _result(current, [history_present, history_missing])
        self.assertEqual(
            _dimension(_analogue(result, "present"), "rsi_end_value")["state"],
            "missing_current",
        )
        self.assertEqual(
            _dimension(_analogue(result, "missing"), "rsi_end_value")["state"],
            "missing_both",
        )

    def test_result_limit_does_not_change_eligibility(self) -> None:
        current = _rich(_current())
        histories = [
            _rich(_historical(case_id=f"case-{index}", episode_id=f"episode-{index}"))
            for index in range(3)
        ]
        result = _result(current, histories, limit=2)
        self.assertEqual(result["eligible_candidate_count"], 3)
        self.assertEqual(result["presented_analogue_count"], 2)
        self.assertEqual(result["not_presented_due_to_limit"], 1)

    def test_repeated_run_is_byte_deterministic_and_does_not_mutate_input(self) -> None:
        comparisons = _comparison_set()
        before = deepcopy(comparisons)
        first = retrieve_analogues(comparisons)
        second = retrieve_analogues(comparisons)
        self.assertEqual(first, second)
        self.assertEqual(comparisons, before)
        self.assertEqual(first["content_hash"], analogue_retrieval_content_hash(first))


class LeakageAndExclusionTests(unittest.TestCase):
    def test_structurally_excluded_case_is_never_retrieved(self) -> None:
        current = _rich(_current("triangle.b"))
        history = _rich(_historical("zigzag.b"))
        result = _result(current, [history])
        self.assertEqual(result["ordered_analogue_references"], [])
        self.assertEqual(result["excluded_candidate_count"], 1)
        self.assertEqual(
            result["excluded_candidate_references"][0]["rule"],
            "HARD-STRUCTURAL-INCOMPATIBILITY",
        )

    def test_cutoff_leakage_is_excluded_before_retrieval(self) -> None:
        history = _rich(_historical())
        history["endpoint_dna"]["cutoff"] = "2026-02-01T00:00:00+00:00"
        _rehash(history)
        result = _result(histories=[history])
        self.assertEqual(result["ordered_analogue_references"], [])
        self.assertEqual(
            result["excluded_candidate_references"][0]["rule"],
            "DNA-ENDPOINT-CUTOFF-LEAKAGE",
        )

    def test_reviewed_outcome_changes_nothing_in_full_pipeline(self) -> None:
        current = _rich(_current())
        history = _rich(_historical())
        baseline_filter = filter_structurally_comparable_cases(current, [history])
        baseline_comparison = compare_filtered_experiences(
            current, [history], baseline_filter
        )
        baseline_retrieval = retrieve_analogues(baseline_comparison)

        attacked_current = deepcopy(current)
        attacked_history = deepcopy(history)
        attacked_current["reviewed_outcome"] = {"secret": "current-outcome-a"}
        attacked_current["confirmation_dna"] = {"secret": "future-current"}
        attacked_history["reviewed_outcome"] = {"secret": "history-outcome-b"}
        attacked_history["resolved_outcome_dna"] = {"secret": "future-history"}
        attacked_filter = filter_structurally_comparable_cases(
            attacked_current, [attacked_history]
        )
        attacked_comparison = compare_filtered_experiences(
            attacked_current, [attacked_history], attacked_filter
        )
        attacked_retrieval = retrieve_analogues(attacked_comparison)

        self.assertEqual(baseline_filter, attacked_filter)
        self.assertEqual(baseline_comparison, attacked_comparison)
        self.assertEqual(baseline_retrieval, attacked_retrieval)
        self.assertNotIn("current-outcome-a", json.dumps(attacked_retrieval))
        self.assertNotIn("history-outcome-b", json.dumps(attacked_retrieval))

    def test_no_outcome_or_confirmation_payload_is_returned(self) -> None:
        serialized = json.dumps(_result(), sort_keys=True)
        self.assertNotIn('"resolved_outcome_dna"', serialized)
        self.assertNotIn('"confirmation_dna"', serialized)
        self.assertNotIn('"reviewed_outcome":', serialized)


class RenderingCliAndStoreTests(unittest.TestCase):
    def test_renderer_distinguishes_tier_evidence_missing_and_warnings(self) -> None:
        history = _remove_value(
            _rich(_historical(case_id="partial", episode_id="partial-episode")),
            "pivot_path",
            "start_alignment",
        )
        rendered = render_analogue_retrieval(
            _result(histories=[history]),
            explain=True,
            show_provenance=True,
            show_hashes=True,
            show_ordering_keys=True,
            include_comparison_details=True,
        )
        self.assertIn("# Experience Analogue Retrieval", rendered)
        self.assertIn("Structural eligibility:", rendered)
        self.assertIn("Retrieval tier:", rendered)
        self.assertIn("Missing dimensions:", rendered)
        self.assertIn("Ordering explanation:", rendered)
        self.assertIn("| Ordering key |", rendered)
        self.assertIn("| Dimension | State |", rendered)
        self.assertIn("Provenance:", rendered)
        self.assertIn("Retrieval result hash:", rendered)

    def test_policy_renderer_is_deterministic(self) -> None:
        first = render_retrieval_policy(retrieval_policy())
        second = render_retrieval_policy(retrieval_policy())
        self.assertEqual(first, second)
        self.assertIn("# Experience Retrieval Policy", first)
        self.assertIn("Final tie-breaker:", first)

    def test_cli_parser_exposes_phase5a4_controls(self) -> None:
        args = build_parser().parse_args(
            [
                "experience",
                "retrieve",
                "current-case",
                "--candidate",
                "history-case",
                "--policy",
                DEFAULT_RETRIEVAL_POLICY_ID,
                "--policy-version",
                DEFAULT_RETRIEVAL_POLICY_VERSION,
                "--comparison-level",
                "3",
                "--limit",
                "5",
                "--format",
                "text",
                "--explain",
                "--show-provenance",
                "--show-hashes",
                "--show-ordering-keys",
                "--include-comparison-details",
            ]
        )
        self.assertEqual(args.experience_command, "retrieve")
        self.assertEqual(args.comparison_level, 3)
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.include_comparison_details)

    def test_cli_json_output(self) -> None:
        result = _result()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.retrieve_experience_analogues.return_value = result
            output = io.StringIO()
            with patch("sys.stdout", output):
                main(["experience", "retrieve", "current-case", "--format", "json"])
        self.assertIn(ANALOGUE_RETRIEVAL_SCHEMA_VERSION, output.getvalue())
        self.assertIn('"ordered_analogue_references"', output.getvalue())

    def test_cli_text_explanation_and_provenance_output(self) -> None:
        result = _result()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.retrieve_experience_analogues.return_value = result
            output = io.StringIO()
            with patch("sys.stdout", output):
                main(
                    [
                        "experience",
                        "retrieve",
                        "current-case",
                        "--format",
                        "text",
                        "--explain",
                        "--show-provenance",
                        "--show-hashes",
                        "--show-ordering-keys",
                        "--include-comparison-details",
                    ]
                )
        rendered = output.getvalue()
        self.assertIn("# Experience Analogue Retrieval", rendered)
        self.assertIn("Ordering explanation:", rendered)
        self.assertIn("Provenance:", rendered)
        self.assertIn("Retrieval result hash:", rendered)

    def test_cli_retrieval_policy_json_and_text_output(self) -> None:
        policy = retrieval_policy()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.experience_retrieval_policy.return_value = policy
            json_output = io.StringIO()
            with patch("sys.stdout", json_output):
                main(["experience", "retrieval-policy", "--format", "json"])
            text_output = io.StringIO()
            with patch("sys.stdout", text_output):
                main(["experience", "retrieval-policy", "--format", "text"])
        self.assertIn(DEFAULT_RETRIEVAL_POLICY_ID, json_output.getvalue())
        self.assertIn("# Experience Retrieval Policy", text_output.getvalue())

    def test_store_empty_pool_and_sqlite_integrity_without_schema_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "experience.sqlite3")
            fixture = _persist_phase4_fixture(store)
            created = store.create_experience_case(str(fixture["case"]["case_id"]))
            case_id = str(created["experience_case_id"])
            with store.connect() as connection:
                tables_before = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            result = store.retrieve_experience_analogues(case_id)
            with store.connect() as connection:
                tables_after = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(
                result["status"], "unavailable_empty_accepted_experience_pool"
            )
            self.assertEqual(tables_before, tables_after)
            self.assertEqual(integrity, "ok")
            self.assertEqual(foreign_keys, [])


if __name__ == "__main__":
    unittest.main()
