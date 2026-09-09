from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from elliott_ai.cli import build_parser
from elliott_ai.experience import (
    DNA_GROUP_NAMES,
    PATTERN_DNA_CALCULATION_VERSION,
    PATTERN_DNA_SCHEMA_VERSION,
    pattern_dna_content_hash,
)
from elliott_ai.experience_comparison import (
    COMPATIBILITY_CLASSES,
    COMPATIBILITY_MATRIX_VERSION,
    FAMILY_CLASSES,
    FAMILY_COMPATIBILITY_MATRIX,
    ROLE_CLASSES,
    ROLE_CLASS_DEFINITIONS,
    ROLE_COMPATIBILITY_MATRIX,
    ComparisonConfig,
    classify_structural_role,
    compatibility_matrix_manifest,
    family_compatibility,
    filter_structurally_comparable_cases,
    role_compatibility,
    validate_endpoint_dna,
)
from elliott_ai.knowledge import KnowledgeStore


CUTOFF = "2026-01-31T00:00:00+00:00"


def _group(
    *,
    status: str = "unavailable",
    values: object = None,
    cutoff: str = CUTOFF,
) -> dict[str, object]:
    return {
        "status": status,
        "values": values,
        "units": {},
        "source_references": [{"source_type": "test_fixture"}],
        "calculation_version": "test-fixture-1.0.0",
        "cutoff": cutoff,
        "unavailable_reason": "Fixture omitted this optional group." if status == "unavailable" else None,
        "incomparable_reason": None,
    }


def _endpoint_dna(
    *,
    role_class: str,
    degree: str = "Minor",
    provider: str = "tradingview",
    feed: str = "tv|NASDAQ:TEST|daily|regular",
    timeframe: str = "daily",
    venue: str = "NASDAQ",
    market_type: str = "equity",
    quote_currency: str = "USD",
    regime: str = "trending",
    volatility_state: str = "stable",
    liquidity: str = "high",
    derivative_type: str = "spot",
    mandatory_provenance_status: str = "compatible",
    endpoint: str = CUTOFF,
) -> dict[str, object]:
    definition = ROLE_CLASS_DEFINITIONS[role_class]
    family = str(definition["family"])
    position = str(definition["position"])
    internal = definition.get("internal_family")
    groups = {name: _group() for name in DNA_GROUP_NAMES}
    groups["source_identity"] = _group(
        status="available",
        values={
            "source": {
                "provider": provider,
                "feed_identity": feed,
                "timeframe": timeframe,
                "venue": venue,
                "market_type": market_type,
                "quote_currency": quote_currency,
                "derivative_type": derivative_type,
                "mandatory_provenance_status": mandatory_provenance_status,
            },
            "identity": {
                "wave_id": "fixture-wave",
                "wave_label": position,
                "wave_degree": degree,
                "parent_pattern_family": family,
                "timeframe": timeframe,
                "end_timestamp": endpoint,
            },
        },
    )
    groups["structural_context"] = _group(
        status="available",
        values={
            "role_class": role_class,
            "candidate_role": position,
            "elliott_degree": degree,
            "parent_pattern_family": family,
            "internal_structure_family": internal,
            "completion_status_at_cutoff": "completed",
        },
    )
    groups["duration"] = _group(
        status="available", values={"candles": 21, "clock_days": 30.0}
    )
    groups["price_shape"] = _group(
        status="available",
        values={"percentage_change": 20.0, "directional_efficiency": 0.7},
    )
    groups["market_context"] = _group(
        status="partial",
        values={
            "market_type": market_type,
            "quote_currency": quote_currency,
            "venue": venue,
            "regime": regime,
            "broader_regime": regime,
            "liquidity": liquidity,
            "derivative_type": derivative_type,
        },
    )
    groups["volatility"] = _group(
        status="available", values={"volatility_state": volatility_state}
    )
    groups["availability_and_comparability"] = _group(
        status="available", values={"data_quality": "sufficient"}
    )
    groups["evidence_references"] = _group(
        status="available", values={"items": []}
    )
    dna: dict[str, object] = {
        "schema_version": PATTERN_DNA_SCHEMA_VERSION,
        "calculation_version": PATTERN_DNA_CALCULATION_VERSION,
        "dna_kind": "endpoint",
        "experience_case_id": "fixture-case",
        "market_episode_id": "fixture-episode",
        "cutoff": CUTOFF,
        "source_fingerprint_hash": "f" * 64,
        "source_endpoint_snapshot_id": "snapshot-fixture",
        "source_endpoint_snapshot_hash": "s" * 64,
        "groups": groups,
        "source_field_map": {},
        "policy": "Endpoint-only test fixture.",
    }
    dna["content_hash"] = pattern_dna_content_hash(dna)
    dna["dna_id"] = f"pattern_dna_{str(dna['content_hash'])[:32]}"
    dna["content_hash"] = pattern_dna_content_hash(dna)
    return dna


def _case(
    case_id: str,
    episode_id: str,
    dna: dict[str, object],
    *,
    state: str = "accepted",
    quality: str = "high",
    active: bool = True,
) -> dict[str, object]:
    return {
        "experience_case_id": case_id,
        "market_episode_id": episode_id,
        "case_version": 1,
        "state": state,
        "quality_status": quality,
        "active_version": active,
        "accepted_pool_eligible": state == "accepted" and quality != "quarantined" and active,
        "experience_schema_version": "elliott-experience-1.0.0",
        "endpoint_dna": dna,
    }


def _current(
    role: str = "impulse.wave_1", *, degree: str = "Minor", **dna_options: object
) -> dict[str, object]:
    return _case(
        "current-case",
        "current-episode",
        _endpoint_dna(role_class=role, degree=degree, **dna_options),
        state="pending_experience_review",
    )


def _historical(
    role: str = "impulse.wave_1",
    *,
    degree: str = "Minor",
    case_id: str = "history-case",
    episode_id: str = "history-episode",
    **options: object,
) -> dict[str, object]:
    return _case(
        case_id,
        episode_id,
        _endpoint_dna(role_class=role, degree=degree, **options),
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


class RoleAndMatrixTests(unittest.TestCase):
    def test_every_required_role_class_is_explicit_and_classifiable(self) -> None:
        required = {
            *(f"impulse.wave_{position}" for position in range(1, 6)),
            *(f"zigzag.{position}" for position in "abc"),
            *(f"flat.{position}" for position in "abc"),
            *(f"triangle.{position}" for position in "abcde"),
            "double_three.w",
            "double_three.x",
            "double_three.y",
            "triple_three.w",
            "triple_three.x",
            "triple_three.y",
            "triple_three.x2",
            "triple_three.z",
        }
        self.assertTrue(required.issubset(ROLE_CLASS_DEFINITIONS))
        for role_class, definition in ROLE_CLASS_DEFINITIONS.items():
            with self.subTest(role_class=role_class):
                classified = classify_structural_role(
                    parent_family=definition["family"],
                    position=definition["position"],
                    internal_family=definition.get("internal_family"),
                    declared_role_class=role_class,
                )
                self.assertEqual(classified["role_class"], role_class)

    def test_role_matrix_is_exhaustive_and_versioned(self) -> None:
        self.assertEqual(set(ROLE_COMPATIBILITY_MATRIX), set(ROLE_CLASSES))
        self.assertTrue(
            all(set(row) == set(ROLE_CLASSES) for row in ROLE_COMPATIBILITY_MATRIX.values())
        )
        manifest = compatibility_matrix_manifest()
        self.assertEqual(manifest["compatibility_matrix_version"], COMPATIBILITY_MATRIX_VERSION)
        self.assertEqual(sum(manifest["role_matrix_counts"].values()), len(ROLE_CLASSES) ** 2)

    def test_family_matrix_is_exhaustive(self) -> None:
        self.assertEqual(set(FAMILY_COMPATIBILITY_MATRIX), set(FAMILY_CLASSES))
        self.assertTrue(
            all(set(row) == set(FAMILY_CLASSES) for row in FAMILY_COMPATIBILITY_MATRIX.values())
        )

    def test_every_compatibility_class_has_an_explicit_example(self) -> None:
        examples = {
            "exact_match": role_compatibility("zigzag.c", "zigzag.c"),
            "compatible": role_compatibility("zigzag.c", "flat.c"),
            "context_only": role_compatibility("impulse.wave_1", "corrective_a.impulse"),
            "incompatible": role_compatibility("triangle.b", "zigzag.b"),
        }
        self.assertEqual(
            {name: result["compatibility_class"] for name, result in examples.items()},
            {name: name for name in COMPATIBILITY_CLASSES},
        )

    def test_family_compatibility_rules(self) -> None:
        self.assertEqual(
            family_compatibility("double_three", "triple_three")["compatibility_class"],
            "compatible",
        )
        self.assertEqual(
            family_compatibility("flat", "zigzag")["compatibility_class"],
            "context_only",
        )
        self.assertEqual(
            family_compatibility("impulse", "triangle")["compatibility_class"],
            "incompatible",
        )

    def test_x_and_x2_remain_structurally_distinct(self) -> None:
        rule = role_compatibility("double_three.x", "triple_three.x2")
        self.assertEqual(rule["compatibility_class"], "incompatible")
        self.assertEqual(rule["rule_id"], "ROLE-COMBINATION-POSITION-MISMATCH")

    def test_triangle_isolation(self) -> None:
        self.assertEqual(
            role_compatibility("triangle.b", "zigzag.b")["compatibility_class"],
            "incompatible",
        )
        self.assertEqual(
            role_compatibility("triangle.b", "triangle.b")["compatibility_class"],
            "exact_match",
        )


class ProgressiveFilteringTests(unittest.TestCase):
    def test_all_four_progressive_search_levels(self) -> None:
        cases = (
            (_historical(), ComparisonConfig(), 1),
            (_historical(degree="Intermediate"), ComparisonConfig(), 2),
            (
                _historical("triple_three.y"),
                ComparisonConfig(),
                3,
            ),
            (
                _historical(market_type="crypto", venue="BINANCE", feed="binance|TESTUSDT|daily", derivative_type="spot"),
                ComparisonConfig(allow_cross_market=True),
                4,
            ),
        )
        currents = (
            _current(),
            _current(),
            _current("double_three.y"),
            _current(),
        )
        for current, (historical, config, expected) in zip(currents, cases):
            with self.subTest(level=expected):
                result = filter_structurally_comparable_cases(
                    current, [historical], config=config
                )
                self.assertEqual(result["eligible"][0]["comparison_level"], expected)

    def test_self_and_same_episode_are_excluded(self) -> None:
        current = _current()
        self_match = deepcopy(current)
        self_match["state"] = "accepted"
        self_match["accepted_pool_eligible"] = True
        same_episode = _historical(
            case_id="other-version", episode_id="current-episode"
        )
        result = filter_structurally_comparable_cases(
            current, [self_match, same_episode]
        )
        rules = {item["rule"] for item in result["excluded"]}
        self.assertEqual(
            rules, {"HARD-SELF-MATCH", "HARD-SAME-ACTIVE-EPISODE"}
        )

    def test_adjacent_degree_default_and_configuration_switches(self) -> None:
        adjacent = _historical(degree="Intermediate")
        allowed = filter_structurally_comparable_cases(_current(), [adjacent])
        self.assertEqual(allowed["eligible"][0]["comparison_level"], 2)
        denied = filter_structurally_comparable_cases(
            _current(), [adjacent], config={"same_degree_only": True}
        )
        self.assertEqual(denied["excluded"][0]["rule"], "CONFIG-ADJACENT-DEGREE-DISABLED")
        denied_again = filter_structurally_comparable_cases(
            _current(), [adjacent], config={"allow_adjacent_degree": False}
        )
        self.assertEqual(denied_again["excluded"][0]["rule"], "CONFIG-ADJACENT-DEGREE-DISABLED")

    def test_far_degree_requires_explicit_opt_in(self) -> None:
        far = _historical(degree="Cycle")
        denied = filter_structurally_comparable_cases(_current(), [far])
        self.assertEqual(denied["excluded"][0]["rule"], "HARD-FAR-DEGREE")
        allowed = filter_structurally_comparable_cases(
            _current(), [far], config={"allow_far_degree": True}
        )
        self.assertEqual(allowed["eligible"][0]["comparison_level"], 3)

    def test_double_three_y_and_triple_three_y_are_compatible(self) -> None:
        result = filter_structurally_comparable_cases(
            _current("double_three.y"), [_historical("triple_three.y")]
        )
        self.assertEqual(result["eligible"][0]["compatibility_class"], "compatible")

    def test_wave1_corrective_a_is_context_only_and_opt_in(self) -> None:
        historical = _historical("corrective_a.impulse")
        denied = filter_structurally_comparable_cases(_current(), [historical])
        self.assertEqual(denied["excluded"][0]["compatibility_class"], "context_only")
        allowed = filter_structurally_comparable_cases(
            _current(), [historical], config={"allow_context_family": True}
        )
        self.assertEqual(allowed["eligible"][0]["compatibility_class"], "context_only")
        self.assertEqual(allowed["eligible"][0]["comparison_level"], 3)

    def test_cross_market_requires_opt_in_and_uses_normalized_scope(self) -> None:
        crypto = _historical(
            market_type="crypto",
            venue="BINANCE",
            feed="binance|TESTUSDT|daily",
            quote_currency="USDT",
        )
        denied = filter_structurally_comparable_cases(_current(), [crypto])
        self.assertEqual(denied["excluded"][0]["rule"], "CONFIG-CROSS-MARKET-DISABLED")
        allowed = filter_structurally_comparable_cases(
            _current(), [crypto], config={"allow_cross_market": True}
        )
        item = allowed["eligible"][0]
        self.assertEqual(item["comparison_level"], 4)
        self.assertEqual(item["comparison_scope"], "normalized_endpoint_groups_only")
        self.assertIn("price_shape.raw_price", item["incomparable_groups"])

    def test_deterministic_filtering_uses_stable_identity_order_not_rank(self) -> None:
        candidates = [
            _historical(case_id="z", episode_id="episode-z"),
            _historical(case_id="a", episode_id="episode-a"),
        ]
        first = filter_structurally_comparable_cases(_current(), candidates)
        second = filter_structurally_comparable_cases(_current(), reversed(candidates))
        self.assertEqual(first, second)
        self.assertEqual(
            [item["experience_case_id"] for item in first["eligible"]], ["a", "z"]
        )
        forbidden = {"similarity_score", "rank", "weight", "probability"}
        self.assertFalse(_keys(first).intersection(forbidden))


class ExclusionAndEvidenceTests(unittest.TestCase):
    def test_unreviewed_quarantined_and_inactive_cases_are_excluded(self) -> None:
        unreviewed = _historical(case_id="pending", episode_id="pending-episode")
        unreviewed.update(state="pending_experience_review", accepted_pool_eligible=False)
        quarantined = _historical(case_id="quarantine", episode_id="q-episode")
        quarantined.update(state="quarantined", quality_status="quarantined", accepted_pool_eligible=False)
        inactive = _historical(case_id="old", episode_id="old-episode")
        inactive.update(active_version=False, accepted_pool_eligible=False)
        result = filter_structurally_comparable_cases(
            _current(), [unreviewed, quarantined, inactive]
        )
        self.assertEqual(
            {item["rule"] for item in result["excluded"]},
            {"HARD-UNREVIEWED", "HARD-QUARANTINED", "HARD-INACTIVE-EPISODE-VERSION"},
        )

    def test_unsupported_schema_and_invalid_hash_are_excluded(self) -> None:
        schema_case = _historical(case_id="schema", episode_id="schema-episode")
        schema_case["endpoint_dna"]["schema_version"] = "pattern-dna-99.0.0"
        schema_case["endpoint_dna"]["content_hash"] = pattern_dna_content_hash(
            schema_case["endpoint_dna"]
        )
        hash_case = _historical(case_id="hash", episode_id="hash-episode")
        hash_case["endpoint_dna"]["groups"]["duration"]["values"]["candles"] = 999
        result = filter_structurally_comparable_cases(
            _current(), [schema_case, hash_case]
        )
        self.assertEqual(
            {item["rule"] for item in result["excluded"]},
            {"DNA-UNSUPPORTED-SCHEMA", "DNA-INVALID-HASH"},
        )

    def test_endpoint_cutoff_leakage_is_excluded(self) -> None:
        leaked = _historical()
        leaked["endpoint_dna"]["cutoff"] = "2026-02-01T00:00:00+00:00"
        leaked["endpoint_dna"]["content_hash"] = pattern_dna_content_hash(
            leaked["endpoint_dna"]
        )
        result = filter_structurally_comparable_cases(_current(), [leaked])
        self.assertEqual(result["excluded"][0]["rule"], "DNA-ENDPOINT-CUTOFF-LEAKAGE")

    def test_missing_mandatory_dna_and_provenance_are_excluded(self) -> None:
        missing_group = _historical(case_id="group", episode_id="group-episode")
        missing_group["endpoint_dna"]["groups"].pop("structural_context")
        missing_group["endpoint_dna"]["content_hash"] = pattern_dna_content_hash(
            missing_group["endpoint_dna"]
        )
        bad_feed = _historical(
            case_id="feed", episode_id="feed-episode", feed=""
        )
        result = filter_structurally_comparable_cases(
            _current(), [missing_group, bad_feed]
        )
        self.assertEqual(
            {item["rule"] for item in result["excluded"]},
            {"DNA-MISSING-MANDATORY-GROUP", "HARD-INCOMPATIBLE-MANDATORY-PROVENANCE"},
        )

    def test_feed_mismatch_marks_feed_bound_groups_incomparable(self) -> None:
        other_feed = _historical(
            feed="other|NASDAQ:TEST|daily", venue="BATS"
        )
        result = filter_structurally_comparable_cases(_current(), [other_feed])
        item = result["eligible"][0]
        self.assertIn("volume.raw", item["incomparable_groups"])
        self.assertIn("volume_profile", item["incomparable_groups"])
        self.assertIn("funding", item["incomparable_groups"])
        self.assertIn("basis", item["incomparable_groups"])
        self.assertIn("different_venue", {penalty["code"] for penalty in item["soft_penalties"]})

    def test_missing_optional_indicators_are_metadata_only(self) -> None:
        result = filter_structurally_comparable_cases(_current(), [_historical()])
        item = result["eligible"][0]
        expected_optional = {"momentum", "volume", "optional_rsi"}
        self.assertTrue(expected_optional.issubset(item["missing_groups"]))
        self.assertIn(
            "missing_optional_indicators",
            {penalty["code"] for penalty in item["soft_penalties"]},
        )
        self.assertTrue(item["eligible"])

    def test_filter_ignores_confirmation_and_outcome_payloads(self) -> None:
        base = _historical()
        attacked = deepcopy(base)
        attacked["confirmation_dna"] = {
            "role_class": "triangle.b",
            "cutoff": "2099-01-01",
        }
        attacked["outcome_dna"] = {"resolved_hypothesis": "anything"}
        first = filter_structurally_comparable_cases(_current(), [base])
        second = filter_structurally_comparable_cases(_current(), [attacked])
        self.assertEqual(first, second)

    def test_every_exclusion_has_rule_reason_and_matrix_location(self) -> None:
        result = filter_structurally_comparable_cases(
            _current("triangle.b"), [_historical("zigzag.b")]
        )
        item = result["excluded"][0]
        self.assertTrue(item["rule"])
        self.assertTrue(item["reason"])
        self.assertIn("role[triangle.b][zigzag.b]", item["matrix_location"])

    def test_endpoint_validator_rejects_later_timing_payload(self) -> None:
        dna = _endpoint_dna(role_class="impulse.wave_1")
        dna["groups"]["confirmation_events"] = _group(
            status="available", values={"displacement": True}
        )
        dna["content_hash"] = pattern_dna_content_hash(dna)
        validation = validate_endpoint_dna(dna)
        self.assertFalse(validation["valid"])
        self.assertIn(
            "DNA-LATER-EVIDENCE-LEAKAGE",
            {item["rule"] for item in validation["reasons"]},
        )


class StoreAndCliTests(unittest.TestCase):
    def test_cold_start_has_no_comparison_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "cold.sqlite3")
            self.assertEqual(store.get_accepted_experience_pool(), [])
            self.assertEqual(store.stats()["experience_cases"], 0)
            manifest = store.experience_compatibility_matrix()
            self.assertEqual(manifest["compatibility_matrix_version"], COMPATIBILITY_MATRIX_VERSION)

    def test_cli_exposes_filter_and_matrix_without_persistence_commands(self) -> None:
        parser = build_parser()
        filtered = parser.parse_args(
            [
                "experience",
                "filter",
                "experience-case",
                "--allow-cross-market",
                "--allow-context-family",
            ]
        )
        matrix = parser.parse_args(["experience", "matrix"])
        self.assertEqual(filtered.experience_command, "filter")
        self.assertEqual(matrix.experience_command, "matrix")


if __name__ == "__main__":
    unittest.main()
