from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elliott_ai.cli import build_parser
from elliott_ai.correction_state import (
    advance_correction_snapshot,
    build_reviewed_outcome,
    create_correction_case,
    create_initial_snapshot,
    snapshot_content_hash,
)
from elliott_ai.experience import (
    DNA_GROUP_NAMES,
    EXPERIENCE_SCHEMA_VERSION,
    PATTERN_DNA_SCHEMA_VERSION,
    assemble_experience_candidate,
    experience_case_content_hash,
    market_episode_identity,
    validate_experience_eligibility,
)
from elliott_ai.fingerprints import (
    fingerprint_content_hash,
    generate_wave_fingerprint,
)
from elliott_ai.knowledge import KnowledgeStore


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _date(day: int) -> str:
    return (START + timedelta(days=day)).date().isoformat()


def _candles(
    prices: list[float], *, start_day: int = 0
) -> list[dict[str, object]]:
    return [
        {
            "date": _date(start_day + index),
            "open": price - 0.1,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1_000_000 + index * 15_000,
        }
        for index, price in enumerate(prices)
    ]


def _source() -> dict[str, object]:
    return {
        "provider": "tradingview",
        "exchange": "NASDAQ",
        "venue": "NASDAQ",
        "symbol": "NASDAQ:TEST",
        "market_type": "equity",
        "timeframe": "daily",
        "feed_identity": "tv|NASDAQ:TEST|daily|regular",
        "volume_scope": "consolidated",
        "session": "regular",
    }


def _fingerprint(*, with_rsi: bool = False) -> dict[str, object]:
    prices = [120.0 - index * (20.0 / 39.0) + (0.35 if index % 5 == 0 else 0.0) for index in range(40)]
    prices[0] = 120.0
    prices[-1] = 100.0
    features = ["volume", "ewo", "macd", "volatility"]
    if with_rsi:
        features.append("rsi")
    return generate_wave_fingerprint(
        {
            "wave_id": "minor_y",
            "sequence_position": "Y",
            "degree": "Minor",
            "parent_pattern_family": "double-three",
            "structure": "zigzag",
            "completion_status": "completed",
            "start": {"date": _date(0), "price": 120.0},
            "end": {"date": _date(39), "price": 100.0},
            "child_wave_ids": ["a", "b", "c"],
            "child_structure_status": "valid",
        },
        _candles(prices),
        source_metadata=_source(),
        cutoff=_date(39),
        features=features,
        symbol="NASDAQ:TEST",
        timeframe="daily",
    )


def _case() -> dict[str, object]:
    return create_correction_case(
        symbol="NASDAQ:TEST",
        timeframe="daily",
        candidate_endpoint={
            "date": _date(39),
            "price": 100.0,
            "terminal_label": "Y",
            "terminal_direction": "down",
            "protected_origin": {"date": _date(39), "price": 100.0},
        },
        elliott_degree="Minor",
        parent_pattern_family="double-three",
        source_identity=_source(),
    )


def _five_away_context() -> dict[str, object]:
    pivots = [100.0, 110.0, 104.0, 122.0, 113.0, 128.0]
    return {
        "channel_break": {
            "observed": True,
            "status": "observed",
            "observed_at": _date(42),
            "reason": "The correction channel was broken.",
        },
        "local_structure_break": {
            "observed": True,
            "status": "observed",
            "observed_at": _date(43),
            "reason": "A local swing high was exceeded.",
        },
        "five_wave_move_away": {
            "pivots": [
                {"date": _date(40 + index), "price": price}
                for index, price in enumerate(pivots)
            ],
            "direction": "up",
            "pattern_family": "impulse",
            "hard_rules_valid": True,
        },
    }


def _later_context(*, x2: bool = False) -> dict[str, object]:
    context: dict[str, object] = {
        **_five_away_context(),
        "retracement": {
            "observed": True,
            "observed_at": _date(48),
            "structure": "zigzag",
            "child_count": 3,
            "pivots": [128.0, 119.0, 123.0, 115.0],
        },
        "subsequent_motive_candidate": {
            "observed": True,
            "observed_at": _date(50),
            "hard_rules_valid": True,
            "pivots": [
                {"date": _date(47 + index), "price": price}
                for index, price in enumerate([115.0, 122.0, 118.0, 130.0])
            ],
        },
    }
    if x2:
        context["x2_z_continuation"] = {
            "observed": True,
            "observed_at": _date(50),
            "positions": ["X2", "Z"],
        }
    return context


def _persist_phase4_fixture(
    store: KnowledgeStore, *, with_rsi: bool = False
) -> dict[str, object]:
    fingerprint = _fingerprint(with_rsi=with_rsi)
    fingerprint_saved = store.save_wave_fingerprint(fingerprint)
    case = _case()
    endpoint = create_initial_snapshot(case, fingerprint_references=[fingerprint])
    store.save_correction_case(case, initial_snapshot=endpoint)
    post_prices = [101.0, 103.0, 106.0, 109.0, 112.0, 116.0]
    child_one = advance_correction_snapshot(
        endpoint,
        cutoff=_date(45),
        newly_available_candles=_candles(post_prices, start_day=40),
        structural_context=_five_away_context(),
    )
    store.save_hypothesis_snapshot(child_one)
    child_two = advance_correction_snapshot(
        child_one,
        cutoff=_date(50),
        newly_available_candles=_candles(
            [101.0, 103.0, 106.0, 109.0, 112.0, 116.0, 120.0, 118.0, 115.0, 119.0, 125.0],
            start_day=40,
        ),
        structural_context=_later_context(),
    )
    store.save_hypothesis_snapshot(child_two)
    outcome = build_reviewed_outcome(
        child_two,
        resolved_hypothesis="correction_complete",
        final_reviewed_interpretation="The local W-X-Y completed the correction.",
        resolution_cutoff=_date(60),
        reviewer="Parwa",
        resolution_confidence="high_after_human_review",
        supporting_future_structure=["Five away, corrective retracement, and origin hold."],
        rejected_alternatives=[
            {
                "hypothesis_id": "triple_three_continuation",
                "reason": "No distinct X2 developed before the next motive sequence.",
            }
        ],
    )
    saved_outcome = store.save_reviewed_outcome(outcome)
    current = store.get_current_reviewed_outcome(str(case["case_id"]))
    assert current is not None
    return {
        "fingerprint": fingerprint,
        "fingerprint_saved": fingerprint_saved,
        "case": case,
        "endpoint": endpoint,
        "child_one": child_one,
        "child_two": child_two,
        "outcome_saved": saved_outcome,
        "current_outcome": current,
    }


def _pure_sources() -> dict[str, object]:
    fingerprint = _fingerprint()
    case = _case()
    endpoint = create_initial_snapshot(case, fingerprint_references=[fingerprint])
    child = advance_correction_snapshot(
        endpoint,
        cutoff=_date(45),
        newly_available_candles=_candles([101, 103, 106, 109, 112, 116], start_day=40),
        structural_context=_five_away_context(),
    )
    outcome = build_reviewed_outcome(
        child,
        resolved_hypothesis="correction_complete",
        final_reviewed_interpretation="Reviewed W-X-Y completion.",
        resolution_cutoff=_date(60),
        reviewer="Parwa",
    )
    from elliott_ai.correction_state import build_outcome_review, finalize_outcome_revision

    outcome = finalize_outcome_revision(outcome, 1)
    review = build_outcome_review(
        outcome,
        action="approved",
        reviewer="Parwa",
        review_status="reviewed",
        reviewed_at=_date(60),
    )
    return {
        "fingerprint": fingerprint,
        "case": case,
        "endpoint": endpoint,
        "child": child,
        "outcome": outcome,
        "review": review,
    }


def _rehash_fingerprint_reference(
    fingerprint: dict[str, object], snapshot: dict[str, object]
) -> None:
    fingerprint["content_hash"] = fingerprint_content_hash(fingerprint)
    snapshot["fingerprint_references"][0]["content_hash"] = fingerprint["content_hash"]
    snapshot["fingerprint_references"][0]["feature_schema_version"] = fingerprint[
        "feature_schema_version"
    ]
    snapshot["content_hash"] = snapshot_content_hash(snapshot)


def _all_keys(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            result.add(str(key))
            result.update(_all_keys(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_all_keys(child))
    return result


class ExperiencePureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = _pure_sources()

    def _assemble(self) -> dict[str, object]:
        return assemble_experience_candidate(
            correction_case=self.sources["case"],
            endpoint_snapshot=self.sources["endpoint"],
            fingerprint=self.sources["fingerprint"],
            outcome=self.sources["outcome"],
            outcome_review=self.sources["review"],
            snapshot_lineage=[self.sources["endpoint"], self.sources["child"]],
        )

    def test_deterministic_market_episode_identity(self) -> None:
        first = market_episode_identity(self.sources["case"])
        second = market_episode_identity(deepcopy(self.sources["case"]))
        self.assertEqual(first, second)

    def test_deterministic_case_identity_and_content_hash(self) -> None:
        first = self._assemble()
        second = self._assemble()
        self.assertEqual(
            first["experience_case"]["experience_case_id"],
            second["experience_case"]["experience_case_id"],
        )
        self.assertEqual(
            first["experience_case"]["content_hash"],
            experience_case_content_hash(first["experience_case"]),
        )

    def test_duplicate_snapshots_do_not_change_episode_identity(self) -> None:
        assembled = self._assemble()
        duplicate_lineage = [
            self.sources["endpoint"],
            self.sources["child"],
            deepcopy(self.sources["child"]),
        ]
        duplicate = assemble_experience_candidate(
            correction_case=self.sources["case"],
            endpoint_snapshot=self.sources["endpoint"],
            fingerprint=self.sources["fingerprint"],
            outcome=self.sources["outcome"],
            outcome_review=self.sources["review"],
            snapshot_lineage=duplicate_lineage,
        )
        self.assertEqual(assembled["market_episode_id"], duplicate["market_episode_id"])

    def test_endpoint_dna_contains_no_later_confirmation_fields(self) -> None:
        endpoint_dna = self._assemble()["pattern_dna"]["endpoint"]
        keys = _all_keys(endpoint_dna)
        forbidden = {
            "displacement",
            "five_away_candidate",
            "corrective_retracement_candidate",
            "origin_hold",
            "origin_invalidation",
            "next_motive_candidate",
            "x2_z_continuation",
            "confirmation_events",
        }
        self.assertFalse(keys.intersection(forbidden))

    def test_confirmation_dna_preserves_first_availability_timing(self) -> None:
        confirmation = self._assemble()["pattern_dna"]["confirmation"]
        events = confirmation["groups"]["confirmation_events"]["values"]
        self.assertEqual(events["channel_break"]["observed_at"], _date(42) + "T00:00:00+00:00")
        self.assertEqual(events["channel_break"]["earliest_available_cutoff"], _date(45) + "T00:00:00+00:00")
        self.assertEqual(events["five_away_candidate"]["verification_status"], True)

    def test_resolved_outcome_build_does_not_mutate_endpoint_dna(self) -> None:
        assembled = self._assemble()
        before = deepcopy(assembled["pattern_dna"]["endpoint"])
        _ = assembled["pattern_dna"]["resolved_outcome"]
        self.assertEqual(before, assembled["pattern_dna"]["endpoint"])

    def test_missing_optional_rsi_is_null_and_unavailable(self) -> None:
        rsi = self._assemble()["pattern_dna"]["endpoint"]["groups"]["optional_rsi"]
        self.assertEqual(rsi["status"], "unavailable")
        self.assertIsNone(rsi["values"])
        self.assertTrue(rsi["unavailable_reason"])

    def test_all_endpoint_groups_have_required_contract_fields(self) -> None:
        groups = self._assemble()["pattern_dna"]["endpoint"]["groups"]
        self.assertEqual(set(groups), set(DNA_GROUP_NAMES))
        required = {
            "status",
            "values",
            "units",
            "source_references",
            "calculation_version",
            "cutoff",
            "unavailable_reason",
            "incomparable_reason",
        }
        self.assertTrue(all(set(group) == required for group in groups.values()))

    def test_source_hash_mismatch_is_rejected(self) -> None:
        broken = deepcopy(self.sources["fingerprint"])
        broken["price"]["end_price"] = 999.0
        result = validate_experience_eligibility(
            correction_case=self.sources["case"],
            endpoint_snapshot=self.sources["endpoint"],
            fingerprint=broken,
            outcome=self.sources["outcome"],
            outcome_review=self.sources["review"],
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("fingerprint_hash_mismatch", {item["code"] for item in result["rejection_reasons"]})

    def test_symbol_timeframe_degree_role_and_family_mismatches_are_rejected(self) -> None:
        mutations = (
            ("identity", "symbol", "NYSE:OTHER", "symbol_mismatch"),
            ("identity", "timeframe", "weekly", "timeframe_mismatch"),
            ("identity", "wave_degree", "Primary", "degree_mismatch"),
            ("identity", "wave_label", "C", "candidate_role_mismatch"),
            ("identity", "parent_pattern_family", "zigzag", "parent_family_mismatch"),
        )
        for block, field, value, code in mutations:
            with self.subTest(code=code):
                broken = deepcopy(self.sources["fingerprint"])
                snapshot = deepcopy(self.sources["endpoint"])
                broken[block][field] = value
                if field == "symbol":
                    broken["source"]["symbol"] = value
                if field == "timeframe":
                    broken["source"]["timeframe"] = value
                _rehash_fingerprint_reference(broken, snapshot)
                result = validate_experience_eligibility(
                    correction_case=self.sources["case"],
                    endpoint_snapshot=snapshot,
                    fingerprint=broken,
                    outcome=self.sources["outcome"],
                    outcome_review=self.sources["review"],
                )
                self.assertIn(code, {item["code"] for item in result["rejection_reasons"]})

    def test_cutoff_leakage_is_quarantined(self) -> None:
        broken = deepcopy(self.sources["fingerprint"])
        snapshot = deepcopy(self.sources["endpoint"])
        broken["cutoff"]["timestamp"] = _date(40) + "T00:00:00+00:00"
        _rehash_fingerprint_reference(broken, snapshot)
        result = validate_experience_eligibility(
            correction_case=self.sources["case"],
            endpoint_snapshot=snapshot,
            fingerprint=broken,
            outcome=self.sources["outcome"],
            outcome_review=self.sources["review"],
        )
        self.assertEqual(result["status"], "quarantined")
        self.assertIn("cutoff_leakage", {item["code"] for item in result["quarantine_reasons"]})

    def test_unsupported_schema_is_quarantined(self) -> None:
        broken = deepcopy(self.sources["fingerprint"])
        snapshot = deepcopy(self.sources["endpoint"])
        broken["feature_schema_version"] = "wave-fingerprint-99.0.0"
        _rehash_fingerprint_reference(broken, snapshot)
        result = validate_experience_eligibility(
            correction_case=self.sources["case"],
            endpoint_snapshot=snapshot,
            fingerprint=broken,
            outcome=self.sources["outcome"],
            outcome_review=self.sources["review"],
        )
        self.assertEqual(result["status"], "quarantined")


class ExperiencePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "experience.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.fixture = _persist_phase4_fixture(self.store)

    def _create(self) -> dict[str, object]:
        return self.store.create_experience_case(str(self.fixture["case"]["case_id"]))

    def test_idempotent_candidate_creation(self) -> None:
        first = self._create()
        second = self._create()
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(first["experience_case_id"], second["experience_case_id"])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM experience_cases").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM pattern_dna").fetchone()[0], 3)

    def test_valid_outcome_is_pending_until_human_acceptance(self) -> None:
        created = self._create()
        status = self.store.get_experience_case_status(created["experience_case_id"])
        self.assertEqual(status["state"], "pending_experience_review")
        self.assertFalse(status["accepted_pool_eligible"])
        with self.assertRaises(ValueError):
            self.store.review_experience_case(
                created["experience_case_id"],
                action="accept",
                reviewer="model:gpt",
                rationale="Model recommendation only.",
                human_confirmed=False,
            )

    def test_named_human_acceptance_enters_pool(self) -> None:
        created = self._create()
        self.store.review_experience_case(
            created["experience_case_id"],
            action="accept",
            reviewer="Parwa",
            rationale="I reviewed the source charts and outcome lineage.",
            human_confirmed=True,
            reviewed_at=_date(61),
        )
        pool = self.store.get_accepted_experience_pool()
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["experience_case_id"], created["experience_case_id"])

    def test_reject_and_quarantine_workflows(self) -> None:
        created = self._create()
        rejected = self.store.review_experience_case(
            created["experience_case_id"],
            action="reject",
            reviewer="Parwa",
            rationale="The structural anchor needs correction.",
            reviewed_at=_date(61),
        )
        self.assertEqual(rejected["effective_status"]["state"], "rejected")
        quarantined = self.store.review_experience_case(
            created["experience_case_id"],
            action="quarantine",
            reviewer="Parwa",
            rationale="Feed provenance is under review.",
            reviewed_at=_date(62),
        )
        self.assertEqual(quarantined["effective_status"]["state"], "quarantined")
        self.assertEqual(len(self.store.get_experience_review_history(created["experience_case_id"])), 2)

    def test_review_revision_preserves_history(self) -> None:
        created = self._create()
        rejected = self.store.review_experience_case(
            created["experience_case_id"],
            action="reject",
            reviewer="Parwa",
            rationale="Initial concern.",
            reviewed_at=_date(61),
        )
        revised = self.store.revise_experience_review(
            created["experience_case_id"],
            parent_review_id=rejected["review_id"],
            effective_action="accept",
            reviewer="Parwa",
            rationale="Lower-timeframe evidence resolved the concern.",
            human_confirmed=True,
            reviewed_at=_date(62),
        )
        history = self.store.get_experience_review_history(created["experience_case_id"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["parent_review_id"], history[0]["review_id"])
        self.assertEqual(history[1]["supersedes_review_id"], history[0]["review_id"])
        self.assertEqual(revised["effective_status"]["state"], "accepted")

    def test_quality_assignment_records_rule_reasons_and_review(self) -> None:
        created = self._create()
        inspected = self.store.inspect_experience_case(created["experience_case_id"])
        proposed = inspected["experience_case"]["proposed_quality"]
        criteria = {item["criterion"] for item in proposed["reasons"]}
        self.assertIn("structural_anchor_completeness", criteria)
        self.assertIn("optional_feature_gaps", criteria)
        assigned = self.store.assign_experience_quality(
            created["experience_case_id"],
            quality_status="medium",
            reviewer="Parwa",
            rationale="Good anchors; optional context remains incomplete.",
            reviewed_at=_date(61),
        )
        self.assertEqual(assigned["effective_status"]["quality_status"], "medium")
        self.assertIsNone(proposed["predictive_score"])

    def test_append_only_tag_add_and_remove(self) -> None:
        created = self._create()
        added = self.store.tag_experience_case(
            created["experience_case_id"],
            tag="canonical:expanded_flat",
            action="add",
            actor="Parwa",
            assigned_at=_date(61),
        )
        removed = self.store.tag_experience_case(
            created["experience_case_id"],
            tag="canonical:expanded_flat",
            action="remove",
            actor="Parwa",
            rationale="Reclassified after review.",
            assigned_at=_date(62),
        )
        tags = self.store.get_experience_tags(created["experience_case_id"])
        self.assertEqual(tags["active_tags"], [])
        self.assertEqual(len(tags["history"]), 2)
        self.assertEqual(removed["parent_assignment_id"], added["assignment_id"])

    def test_outcome_revision_creates_new_version_and_supersedes_old(self) -> None:
        first = self._create()
        self.store.review_experience_case(
            first["experience_case_id"],
            action="accept",
            reviewer="Parwa",
            rationale="Accepted version one.",
            human_confirmed=True,
            reviewed_at=_date(61),
        )
        latest_snapshot = self.store.get_latest_hypothesis_snapshot(str(self.fixture["case"]["case_id"]))
        assert latest_snapshot is not None
        revised_outcome = build_reviewed_outcome(
            latest_snapshot,
            resolved_hypothesis="larger_wave_w_complete",
            final_reviewed_interpretation="The same local W-X-Y was later resolved as a larger W.",
            resolution_cutoff=_date(70),
            reviewer="Parwa",
            rejected_alternatives=[
                {
                    "hypothesis_id": "correction_complete",
                    "reason": "A later X and Y extended the same-degree correction.",
                }
            ],
        )
        self.store.revise_outcome_resolution(revised_outcome)
        second = self._create()
        self.assertTrue(second["inserted"])
        self.assertEqual(second["case_version"], 2)
        self.assertEqual(second["market_episode_id"], first["market_episode_id"])
        self.assertEqual(second["supersedes_case_id"], first["experience_case_id"])
        self.assertEqual(
            self.store.get_experience_case_status(first["experience_case_id"])["state"],
            "superseded",
        )
        self.assertEqual(self.store.get_accepted_experience_pool(), [])

    def test_superseding_version_does_not_modify_old_endpoint_dna(self) -> None:
        first = self._create()
        before = deepcopy(self.store.get_pattern_dna(first["experience_case_id"], dna_kind="endpoint"))
        latest_snapshot = self.store.get_latest_hypothesis_snapshot(str(self.fixture["case"]["case_id"]))
        assert latest_snapshot is not None
        revised = build_reviewed_outcome(
            latest_snapshot,
            resolved_hypothesis="larger_wave_a_complete",
            final_reviewed_interpretation="Later evidence resolved the local W-X-Y as a larger A.",
            resolution_cutoff=_date(70),
            reviewer="Parwa",
        )
        self.store.revise_outcome_resolution(revised)
        self._create()
        after = self.store.get_pattern_dna(first["experience_case_id"], dna_kind="endpoint")
        self.assertEqual(before, after)

    def test_export_contains_one_active_observation_per_episode(self) -> None:
        first = self._create()
        latest_snapshot = self.store.get_latest_hypothesis_snapshot(str(self.fixture["case"]["case_id"]))
        assert latest_snapshot is not None
        revised = build_reviewed_outcome(
            latest_snapshot,
            resolved_hypothesis="larger_wave_w_complete",
            final_reviewed_interpretation="Revised as larger W.",
            resolution_cutoff=_date(70),
            reviewer="Parwa",
        )
        self.store.revise_outcome_resolution(revised)
        second = self._create()
        exported = self.store.export_experience_cases()
        self.assertEqual(exported["episode_count"], 1)
        self.assertEqual(exported["case_count"], 1)
        self.assertEqual(
            exported["cases"][0]["effective_status"]["experience_case_id"],
            second["experience_case_id"],
        )
        self.assertNotEqual(first["experience_case_id"], second["experience_case_id"])

    def test_database_integrity_and_zero_foreign_key_violations(self) -> None:
        self._create()
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


class ExperienceColdStartAndMigrationTests(unittest.TestCase):
    def test_no_automatic_backfill_and_empty_accepted_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "cold.sqlite3")
            self.assertEqual(store.list_experience_candidates(), [])
            self.assertEqual(store.get_accepted_experience_pool(), [])
            self.assertEqual(store.stats()["experience_cases"], 0)

    def test_existing_records_remain_readable_and_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE legacy_records(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO legacy_records(payload) VALUES ('preserve-me')")
            connection.commit()
            connection.close()
            first = KnowledgeStore(database)
            self.assertIsNotNone(first.phase5a1_backup_path)
            self.assertTrue(first.phase5a1_backup_path.exists())
            self.assertTrue(first.phase5a1_migration_audit_path.exists())
            second = KnowledgeStore(database)
            self.assertIsNone(second.phase5a1_backup_path)
            check = sqlite3.connect(database)
            try:
                self.assertEqual(
                    check.execute("SELECT payload FROM legacy_records").fetchone()[0],
                    "preserve-me",
                )
                self.assertEqual(check.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                check.close()

    def test_cli_exposes_required_phase5a1_operations(self) -> None:
        parser = build_parser()
        commands = (
            ["experience", "candidates"],
            ["experience", "create", "correction_1"],
            ["experience", "inspect", "experience_1"],
            ["experience", "review", "experience_1", "--action", "reject", "--reviewer", "Parwa", "--rationale", "bad anchor"],
            ["experience", "revise-review", "experience_1", "--parent-review", "review_1", "--action", "accept", "--reviewer", "Parwa", "--rationale", "resolved", "--human-confirmed"],
            ["experience", "quality", "experience_1", "--status", "high", "--reviewer", "Parwa", "--rationale", "complete"],
            ["experience", "tag", "experience_1", "--tag", "market:equity", "--action", "add", "--actor", "Parwa"],
            ["experience", "dna", "experience_1", "--kind", "endpoint"],
            ["experience", "export", "--output", "experience.json"],
        )
        parsed = [parser.parse_args(command) for command in commands]
        self.assertTrue(all(item.command == "experience" for item in parsed))

    def test_schema_versions_and_tables_are_present_but_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "schema.sqlite3")
            with store.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(
                    {
                        "experience_schema_versions",
                        "experience_cases",
                        "pattern_dna",
                        "experience_tags",
                        "experience_case_tags",
                        "experience_reviews",
                    }.issubset(tables)
                )
                versions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT schema_version FROM experience_schema_versions"
                    )
                }
                self.assertIn(EXPERIENCE_SCHEMA_VERSION, versions)
                self.assertIn(PATTERN_DNA_SCHEMA_VERSION, versions)
                self.assertEqual(connection.execute("SELECT count(*) FROM experience_cases").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
