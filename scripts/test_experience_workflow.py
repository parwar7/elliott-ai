from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from elliott_ai.cli import build_parser, main
from elliott_ai.correction_state import (
    build_reviewed_outcome,
    correction_case_content_hash,
    create_correction_case,
    create_initial_snapshot,
    snapshot_content_hash,
)
from elliott_ai.experience_workflow import (
    EXPERIENCE_WORKFLOW_MIGRATION_SQL,
    OUTCOME_REVIEW_VERSION,
    STRUCTURAL_REVIEW_VERSION,
    WORKFLOW_SPEC_VERSION,
    WORKFLOW_STATES,
    assemble_workflow_draft,
    build_workflow_event,
    validate_event_chain,
    validate_workflow_sources,
    workflow_case_content_hash,
    workflow_event_content_hash,
    workflow_specification,
)
from elliott_ai.fingerprints import (
    fingerprint_content_hash,
    generate_wave_fingerprint,
)
from elliott_ai.knowledge import KnowledgeStore


START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _date(day: int) -> str:
    return (START + timedelta(days=day)).date().isoformat()


def _source(symbol: str) -> dict[str, object]:
    return {
        "provider": "tradingview",
        "exchange": "NASDAQ",
        "venue": "NASDAQ",
        "symbol": symbol,
        "market_type": "equity",
        "timeframe": "daily",
        "feed_identity": f"tv|{symbol}|daily|regular",
        "volume_scope": "consolidated",
        "session": "regular",
    }


def _candles(symbol: str, offset: int) -> list[dict[str, object]]:
    del symbol
    prices = [120.0 - index * (20.0 / 39.0) for index in range(40)]
    prices[0] = 120.0
    prices[-1] = 100.0
    return [
        {
            "date": _date(offset + index),
            "open": price + 0.2,
            "high": price + 0.6,
            "low": price - 0.6,
            "close": price,
            "volume": 1_000_000 + index * 10_000,
        }
        for index, price in enumerate(prices)
    ]


def _fingerprint(symbol: str, offset: int, *, with_rsi: bool = False) -> dict[str, object]:
    features = ["volume", "ewo", "macd", "volatility"]
    if with_rsi:
        features.append("rsi")
    return generate_wave_fingerprint(
        {
            "wave_id": f"minor_y_{symbol}_{offset}",
            "sequence_position": "Y",
            "degree": "Minor",
            "parent_pattern_family": "double-three",
            "structure": "zigzag",
            "completion_status": "completed",
            "start": {"date": _date(offset), "price": 120.0},
            "end": {"date": _date(offset + 39), "price": 100.0},
            "child_wave_ids": ["a", "b", "c"],
            "child_structure_status": "valid",
        },
        _candles(symbol, offset),
        source_metadata=_source(symbol),
        cutoff=_date(offset + 39),
        features=features,
        symbol=symbol,
        timeframe="daily",
    )


def _persist_fixture(
    store: KnowledgeStore,
    *,
    symbol: str = "NASDAQ:WFLOW",
    offset: int = 0,
    with_outcome: bool = True,
    with_rsi: bool = False,
) -> dict[str, object]:
    fingerprint = _fingerprint(symbol, offset, with_rsi=with_rsi)
    fingerprint_saved = store.save_wave_fingerprint(fingerprint)
    case = create_correction_case(
        symbol=symbol,
        timeframe="daily",
        candidate_endpoint={
            "date": _date(offset + 39),
            "price": 100.0,
            "terminal_label": "Y",
            "terminal_direction": "down",
            "protected_origin": {
                "date": _date(offset + 39),
                "price": 100.0,
            },
        },
        elliott_degree="Minor",
        parent_pattern_family="double-three",
        source_identity=_source(symbol),
    )
    endpoint = create_initial_snapshot(case, fingerprint_references=[fingerprint])
    store.save_correction_case(case, initial_snapshot=endpoint)
    outcome = None
    if with_outcome:
        proposed = build_reviewed_outcome(
            endpoint,
            resolved_hypothesis="correction_complete",
            final_reviewed_interpretation=(
                "The historical endpoint was reviewed as a completed correction."
            ),
            resolution_cutoff=_date(offset + 45),
            reviewer="Parwa",
            supporting_future_structure=["Human-reviewed structural aftermath."],
        )
        store.save_reviewed_outcome(proposed)
        outcome = store.get_current_reviewed_outcome(str(case["case_id"]))
    return {
        "fingerprint": fingerprint,
        "fingerprint_saved": fingerprint_saved,
        "case": case,
        "endpoint": endpoint,
        "outcome": outcome,
    }


def _structural_review(
    *,
    ambiguity: bool = False,
    rejected: bool = False,
    resolves: list[str] | None = None,
) -> dict[str, object]:
    return {
        "structural_role_confirmation": "confirmed",
        "degree_confirmation": "confirmed",
        "endpoint_confirmation": "confirmed",
        "pattern_family_confirmation": "confirmed",
        "rule_validation_notes": "Hard Elliott rules and child structure reviewed.",
        "fibonacci_notes": "Reviewed as supporting evidence only.",
        "rsi_notes": "RSI was not required for structural validity.",
        "volume_notes": "Volume was treated as non-invalidating evidence.",
        "channel_notes": "Channel evidence was reviewed.",
        "multi_timeframe_notes": "Parent and child boundaries were checked.",
        "ambiguity_notes": (
            "Two structurally valid alternatives remain." if ambiguity else "None unresolved."
        ),
        "alternative_counts": (
            [{"label": "larger Wave W", "status": "structurally_valid"}]
            if ambiguity
            else []
        ),
        "rejection_reason_codes": ["hard_rule_failure"] if rejected else [],
        "resolves_event_ids": list(resolves or []),
    }


def _outcome_review(
    *,
    category: str = "reviewed_endpoint_completion",
    ambiguous: bool = False,
    rejected: bool = False,
    resolves: list[str] | None = None,
) -> dict[str, object]:
    return {
        "historical_endpoint_linkage": "confirmed",
        "structural_aftermath": category,
        "confirmation_invalidation_behavior": (
            "unresolved" if ambiguous else "confirmed"
        ),
        "unresolved_state": ambiguous,
        "data_completeness": "partial",
        "feed_continuity": "continuous_to_review_cutoff",
        "reviewer_limitations": [
            "No immutable post-endpoint candle series is stored."
        ],
        "notes": "Historical reviewed evidence only; not a forecast.",
        "rejection_reason_codes": ["outcome_linkage_rejected"] if rejected else [],
        "resolves_event_ids": list(resolves or []),
    }


def _draft_and_validate(
    store: KnowledgeStore, fixture: dict[str, object], *, day: int = 50
) -> str:
    drafted = store.create_experience_workflow_draft(
        str(fixture["case"]["case_id"]), recorded_at=_date(day)
    )
    workflow_case_id = str(drafted["workflow_case"]["workflow_case_id"])
    store.validate_experience_workflow_case(
        workflow_case_id, recorded_at=_date(day + 1)
    )
    return workflow_case_id


def _accept_workflow(
    store: KnowledgeStore, fixture: dict[str, object], *, day: int = 50
) -> tuple[str, dict[str, object]]:
    workflow_case_id = _draft_and_validate(store, fixture, day=day)
    store.review_experience_workflow_structure(
        workflow_case_id,
        decision="accepted",
        reviewer="Parwa",
        review=_structural_review(),
        reviewed_at=_date(day + 2),
    )
    store.review_experience_workflow_outcome(
        workflow_case_id,
        decision="accepted",
        reviewer="Parwa",
        review=_outcome_review(),
        reviewed_at=_date(day + 3),
    )
    result = store.accept_experience_workflow_case(
        workflow_case_id,
        reviewer="Parwa",
        rationale="Explicit final human acceptance after both reviews.",
        reviewed_at=_date(day + 4),
    )
    return workflow_case_id, result


class WorkflowContractAndMigrationTests(unittest.TestCase):
    def test_specification_is_versioned_deterministic_and_explicit(self) -> None:
        first = workflow_specification()
        second = workflow_specification()
        self.assertEqual(first, second)
        self.assertEqual(first["workflow_spec_version"], WORKFLOW_SPEC_VERSION)
        self.assertEqual(first["states"], list(WORKFLOW_STATES))
        self.assertFalse(first["reviewer_policy"]["majority_voting"])
        self.assertTrue(
            first["reviewer_policy"]["unresolved_disagreement_blocks_final_acceptance"]
        )

    def test_exact_two_table_schema_indexes_and_restrict_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            with store.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'experience_workflow_%'"
                    )
                }
                self.assertEqual(
                    tables,
                    {"experience_workflow_cases", "experience_workflow_events"},
                )
                for table in tables:
                    foreign_keys = connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    ).fetchall()
                    self.assertTrue(foreign_keys)
                    self.assertTrue(all(row[6] == "RESTRICT" for row in foreign_keys))
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name LIKE 'experience_workflow_%_idx'"
                    )
                }
            self.assertEqual(len(EXPERIENCE_WORKFLOW_MIGRATION_SQL), 10)
            self.assertEqual(len(indexes), 8)

    def test_existing_database_is_backed_up_and_audited_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy_marker VALUES ('preserve-me')")
            connection.commit()
            connection.close()
            first = KnowledgeStore(database)
            self.assertIsNotNone(first.experience_workflow_backup_path)
            self.assertTrue(first.experience_workflow_backup_path.exists())
            self.assertTrue(first.experience_workflow_migration_audit_path.exists())
            second = KnowledgeStore(database)
            self.assertIsNone(second.experience_workflow_backup_path)
            with second.connect() as migrated:
                self.assertEqual(
                    migrated.execute("SELECT value FROM legacy_marker").fetchone()[0],
                    "preserve-me",
                )

    def test_partial_migration_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "partial.sqlite3"
            store = KnowledgeStore(database)
            with store.connect() as connection:
                connection.execute("DROP TABLE experience_workflow_events")
            with self.assertRaises(RuntimeError):
                KnowledgeStore(database)

    def test_empty_pool_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "empty.sqlite3")
            self.assertEqual(store.discover_experience_workflow_candidates(), [])
            self.assertEqual(store.get_experience_workflow_pool(), [])
            self.assertEqual(store.get_accepted_experience_pool(), [])

    def test_cli_parser_preserves_old_commands_and_adds_workflow_commands(self) -> None:
        parser = build_parser()
        commands = [
            ["experience", "workflow-candidates"],
            ["experience", "workflow-spec"],
            ["experience", "draft", "SOURCE"],
            ["experience", "workflow-inspect", "CASE"],
            ["experience", "validate", "CASE"],
            ["experience", "acceptance-status", "CASE"],
            ["experience", "pool"],
            ["experience", "candidates"],
            ["experience", "retrieval-policy"],
            ["experience", "outcome-spec"],
        ]
        parsed = [parser.parse_args(command) for command in commands]
        self.assertTrue(all(item.command == "experience" for item in parsed))


class WorkflowDiscoveryAndValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / "brain.sqlite3")
        self.fixture = _persist_fixture(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bundle(self) -> dict[str, object]:
        with self.store.connect() as connection:
            return self.store._workflow_source_bundle(
                connection, str(self.fixture["case"]["case_id"])
            )

    def test_candidate_discovery_does_not_require_or_accept_an_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "no-outcome.sqlite3")
            _persist_fixture(store, with_outcome=False)
            candidates = store.discover_experience_workflow_candidates()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["candidate_status"], "eligible_for_draft")
            self.assertFalse(candidates[0]["accepted"])

    def test_valid_draft_assembly_and_hash_stability(self) -> None:
        bundle = self._bundle()
        first = assemble_workflow_draft(**bundle, workflow_version=1)
        second = assemble_workflow_draft(**deepcopy(bundle), workflow_version=1)
        self.assertEqual(first, second)
        self.assertEqual(first["content_hash"], workflow_case_content_hash(first))
        self.assertEqual(first["validation"]["status"], "passed")

    def test_draft_is_immutable_and_duplicate_pair_is_idempotent(self) -> None:
        first = self.store.create_experience_workflow_draft(
            str(self.fixture["case"]["case_id"]), recorded_at=_date(50)
        )
        workflow_case_id = first["workflow_case"]["workflow_case_id"]
        before_events = self.store.get_experience_workflow_events(workflow_case_id)
        second = self.store.create_experience_workflow_draft(
            str(self.fixture["case"]["case_id"]), recorded_at=_date(99)
        )
        after_events = self.store.get_experience_workflow_events(workflow_case_id)
        self.assertFalse(second["created"])
        self.assertTrue(second["duplicate_source_pair"])
        self.assertEqual(before_events, after_events)

    def test_passing_validation_does_not_accept_case(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        inspected = self.store.inspect_experience_workflow_case(workflow_case_id)
        self.assertEqual(inspected["status"]["current_state"], "ready_for_structural_review")
        self.assertFalse(inspected["status"]["accepted_into_existing_pool"])
        self.assertEqual(self.store.get_accepted_experience_pool(), [])

    def test_invalid_stored_hash_is_discovered_as_invalid(self) -> None:
        fingerprint_id = self.fixture["fingerprint_saved"]["fingerprint_id"]
        with self.store.connect() as connection:
            payload = deepcopy(self.fixture["fingerprint"])
            payload["identity"]["symbol"] = "NASDAQ:TAMPERED"
            connection.execute(
                "UPDATE wave_fingerprints SET fingerprint_json = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), fingerprint_id),
            )
        candidates = self.store.discover_experience_workflow_candidates(
            include_invalid=True
        )
        self.assertEqual(candidates[0]["candidate_status"], "validation_failed")
        failed = {
            item["check_id"]
            for item in candidates[0]["validation"]["hard_exclusions"]
        }
        self.assertIn("fingerprint_hash", failed)

    def test_cutoff_violation_is_a_hard_exclusion(self) -> None:
        bundle = self._bundle()
        fingerprint = deepcopy(bundle["fingerprint"])
        fingerprint["cutoff"]["last_source_candle_timestamp"] = _date(100)
        fingerprint["content_hash"] = fingerprint_content_hash(fingerprint)
        snapshot = deepcopy(bundle["endpoint_snapshot"])
        snapshot["fingerprint_references"][0]["content_hash"] = fingerprint["content_hash"]
        snapshot["content_hash"] = snapshot_content_hash(snapshot)
        validation = validate_workflow_sources(
            **{
                **bundle,
                "fingerprint": fingerprint,
                "endpoint_snapshot": snapshot,
                "stored_fingerprint_hash": fingerprint["content_hash"],
                "stored_endpoint_snapshot_hash": snapshot["content_hash"],
            }
        )
        failed = {item["check_id"] for item in validation["hard_exclusions"]}
        self.assertIn("fingerprint_candles_not_after_cutoff", failed)

    def test_identity_and_provenance_mismatches_are_detected(self) -> None:
        cases = {
            "symbol_match": lambda value: value["identity"].__setitem__(
                "symbol", "NASDAQ:OTHER"
            ),
            "timeframe_match": lambda value: value["identity"].__setitem__(
                "timeframe", "weekly"
            ),
            "degree_match": lambda value: value["identity"].__setitem__(
                "wave_degree", "Intermediate"
            ),
            "endpoint_match": lambda value: value["identity"].__setitem__(
                "end_timestamp", _date(38)
            ),
            "role_match": lambda value: value["identity"].__setitem__(
                "wave_label", "C"
            ),
            "source_feed_identity_match": lambda value: value["source"].__setitem__(
                "feed_identity", "different-feed"
            ),
        }
        for expected_check, mutate in cases.items():
            with self.subTest(expected_check=expected_check):
                bundle = self._bundle()
                fingerprint = deepcopy(bundle["fingerprint"])
                mutate(fingerprint)
                fingerprint["content_hash"] = fingerprint_content_hash(fingerprint)
                snapshot = deepcopy(bundle["endpoint_snapshot"])
                snapshot["fingerprint_references"][0]["content_hash"] = fingerprint[
                    "content_hash"
                ]
                snapshot["content_hash"] = snapshot_content_hash(snapshot)
                validation = validate_workflow_sources(
                    **{
                        **bundle,
                        "fingerprint": fingerprint,
                        "endpoint_snapshot": snapshot,
                        "stored_fingerprint_hash": fingerprint["content_hash"],
                        "stored_endpoint_snapshot_hash": snapshot["content_hash"],
                    }
                )
                failed = {
                    item["check_id"] for item in validation["hard_exclusions"]
                }
                self.assertIn(expected_check, failed)

    def test_missing_required_structural_review_fields_block_acceptance(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        review = _structural_review()
        review.pop("rule_validation_notes")
        with self.assertRaises(ValueError):
            self.store.review_experience_workflow_structure(
                workflow_case_id,
                decision="accepted",
                reviewer="Parwa",
                review=review,
            )

    def test_missing_required_source_identity_is_a_hard_failure(self) -> None:
        bundle = self._bundle()
        fingerprint = deepcopy(bundle["fingerprint"])
        fingerprint["source"]["provider"] = ""
        fingerprint["content_hash"] = fingerprint_content_hash(fingerprint)
        snapshot = deepcopy(bundle["endpoint_snapshot"])
        snapshot["fingerprint_references"][0]["content_hash"] = fingerprint[
            "content_hash"
        ]
        snapshot["content_hash"] = snapshot_content_hash(snapshot)
        validation = validate_workflow_sources(
            **{
                **bundle,
                "fingerprint": fingerprint,
                "endpoint_snapshot": snapshot,
                "stored_fingerprint_hash": fingerprint["content_hash"],
                "stored_endpoint_snapshot_hash": snapshot["content_hash"],
            }
        )
        failed = {item["check_id"] for item in validation["hard_exclusions"]}
        self.assertIn("source_provider_match", failed)

    def test_source_records_are_not_mutated_by_draft_or_validation(self) -> None:
        before = self._bundle()
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        after = self._bundle()
        self.assertEqual(before, after)
        self.assertTrue(workflow_case_id)


class WorkflowReviewAndAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / "brain.sqlite3")
        self.fixture = _persist_fixture(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_hashes_and_legal_chain_are_deterministic(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        events = self.store.get_experience_workflow_events(workflow_case_id)
        self.assertTrue(validate_event_chain(events)["valid"])
        for event in events:
            self.assertEqual(event["content_hash"], workflow_event_content_hash(event))

    def test_illegal_state_jump_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_workflow_event(
                workflow_case_id="case",
                sequence_number=2,
                parent_event_id="parent",
                event_kind="final_acceptance",
                from_state="assembled",
                to_state="accepted",
                actor_reference="Parwa",
                reviewer_reference="Parwa",
                human_confirmed=True,
                decision="accepted",
                source_outcome_id="outcome",
                source_outcome_review_id="review",
                result_experience_case_id="experience",
                result_experience_review_id="experience-review",
                recorded_at=_date(1),
            )

    def test_all_structural_review_decisions_use_explicit_states(self) -> None:
        cases = (
            ("accepted", _structural_review(), "structural_review_accepted"),
            ("rejected", _structural_review(rejected=True), "structural_review_rejected"),
            ("needs_revision", _structural_review(), "structural_review_needs_revision"),
            (
                "structurally_ambiguous",
                _structural_review(ambiguity=True),
                "structural_review_ambiguous",
            ),
        )
        for index, (decision, review, expected) in enumerate(cases):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                store = KnowledgeStore(Path(directory) / "brain.sqlite3")
                fixture = _persist_fixture(store, symbol=f"NASDAQ:S{index}", offset=index * 60)
                workflow_case_id = _draft_and_validate(store, fixture, day=index * 60 + 50)
                result = store.review_experience_workflow_structure(
                    workflow_case_id,
                    decision=decision,
                    reviewer="Parwa",
                    review=review,
                    reviewed_at=_date(index * 60 + 52),
                )
                self.assertEqual(result["status"]["current_state"], expected)

    def test_outcome_acceptance_and_rejection_use_separate_states(self) -> None:
        for index, (decision, expected) in enumerate(
            (("accepted", "outcome_review_accepted"), ("rejected", "outcome_review_rejected"))
        ):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                store = KnowledgeStore(Path(directory) / "brain.sqlite3")
                fixture = _persist_fixture(store, symbol=f"NASDAQ:O{index}", offset=index * 60)
                workflow_case_id = _draft_and_validate(store, fixture, day=index * 60 + 50)
                store.review_experience_workflow_structure(
                    workflow_case_id,
                    decision="accepted",
                    reviewer="Parwa",
                    review=_structural_review(),
                    reviewed_at=_date(index * 60 + 52),
                )
                result = store.review_experience_workflow_outcome(
                    workflow_case_id,
                    decision=decision,
                    reviewer="Parwa",
                    review=_outcome_review(rejected=decision == "rejected"),
                    reviewed_at=_date(index * 60 + 53),
                )
                self.assertEqual(result["status"]["current_state"], expected)

    def test_outcome_needs_revision_and_ambiguity_are_explicit(self) -> None:
        for index, (decision, review, expected) in enumerate(
            (
                (
                    "needs_revision",
                    _outcome_review(),
                    "outcome_review_needs_revision",
                ),
                (
                    "structurally_ambiguous",
                    _outcome_review(ambiguous=True),
                    "outcome_review_ambiguous",
                ),
            )
        ):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                store = KnowledgeStore(Path(directory) / "brain.sqlite3")
                fixture = _persist_fixture(store, symbol=f"NASDAQ:OA{index}", offset=index * 60)
                workflow_case_id = _draft_and_validate(store, fixture, day=index * 60 + 50)
                store.review_experience_workflow_structure(
                    workflow_case_id,
                    decision="accepted",
                    reviewer="Parwa",
                    review=_structural_review(),
                )
                result = store.review_experience_workflow_outcome(
                    workflow_case_id,
                    decision=decision,
                    reviewer="Parwa",
                    review=review,
                )
                self.assertEqual(result["status"]["current_state"], expected)
                self.assertEqual(store.get_experience_workflow_pool(), [])

    def test_unavailable_outcome_cannot_be_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fixture = _persist_fixture(store, with_outcome=False)
            workflow_case_id = _draft_and_validate(store, fixture)
            store.review_experience_workflow_structure(
                workflow_case_id,
                decision="accepted",
                reviewer="Parwa",
                review=_structural_review(),
            )
            with self.assertRaises(ValueError):
                store.review_experience_workflow_outcome(
                    workflow_case_id,
                    decision="accepted",
                    reviewer="Parwa",
                    review=_outcome_review(),
                )

    def test_final_acceptance_prerequisites_block_early_acceptance(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        status = self.store.experience_workflow_acceptance_status(workflow_case_id)
        self.assertFalse(status["eligible_for_final_acceptance"])
        with self.assertRaises(ValueError):
            self.store.accept_experience_workflow_case(
                workflow_case_id, reviewer="Parwa", rationale="Too early."
            )

    def test_explicit_final_acceptance_atomically_populates_existing_pool(self) -> None:
        workflow_case_id, result = _accept_workflow(self.store, self.fixture)
        self.assertEqual(result["status"]["current_state"], "accepted")
        self.assertEqual(len(self.store.get_experience_workflow_pool()), 1)
        self.assertEqual(len(self.store.get_accepted_experience_pool()), 1)
        final_event = result["events"][-1]
        self.assertEqual(final_event["event_kind"], "final_acceptance")
        self.assertEqual(final_event["reviewer_reference"], "Parwa")
        self.assertTrue(final_event["human_confirmed"])
        self.assertTrue(final_event["result_experience_case_id"])
        self.assertTrue(final_event["result_experience_review_id"])
        self.assertEqual(workflow_case_id, result["workflow_case"]["workflow_case_id"])

    def test_same_named_human_can_perform_all_three_stages(self) -> None:
        _, result = _accept_workflow(self.store, self.fixture)
        human_events = [
            event
            for event in result["events"]
            if event.get("reviewer_reference") is not None
        ]
        self.assertGreaterEqual(len(human_events), 3)
        self.assertEqual({event["reviewer_reference"] for event in human_events}, {"Parwa"})

    def test_structural_review_never_mutates_phase3_or_phase4_sources(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        with self.store.connect() as connection:
            before = self.store._workflow_source_bundle(
                connection, str(self.fixture["case"]["case_id"])
            )
        self.store.review_experience_workflow_structure(
            workflow_case_id,
            decision="accepted",
            reviewer="Parwa",
            review=_structural_review(),
        )
        with self.store.connect() as connection:
            after = self.store._workflow_source_bundle(
                connection, str(self.fixture["case"]["case_id"])
            )
        self.assertEqual(before, after)

    def test_structural_ambiguity_cannot_enter_pool(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        self.store.review_experience_workflow_structure(
            workflow_case_id,
            decision="structurally_ambiguous",
            reviewer="Parwa",
            review=_structural_review(ambiguity=True),
        )
        self.assertEqual(self.store.get_experience_workflow_pool(), [])
        with self.assertRaises(ValueError):
            self.store.accept_experience_workflow_case(
                workflow_case_id, reviewer="Parwa", rationale="Still ambiguous."
            )

    def test_reviewer_disagreement_blocks_until_explicit_referenced_resolution(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        self.store.review_experience_workflow_structure(
            workflow_case_id,
            decision="accepted",
            reviewer="Reviewer A",
            review=_structural_review(),
        )
        disputed = self.store.review_experience_workflow_structure(
            workflow_case_id,
            decision="rejected",
            reviewer="Reviewer B",
            review=_structural_review(rejected=True),
        )
        self.assertEqual(disputed["status"]["current_state"], "structural_review_ambiguous")
        disagreement_id = disputed["events"][-1]["event_id"]
        self.assertTrue(disputed["status"]["final_acceptance_blocked"])
        with self.assertRaises(ValueError):
            self.store.review_experience_workflow_structure(
                workflow_case_id,
                decision="accepted",
                reviewer="Reviewer C",
                review=_structural_review(),
            )
        resolved = self.store.review_experience_workflow_structure(
            workflow_case_id,
            decision="accepted",
            reviewer="Reviewer C",
            review=_structural_review(resolves=[disagreement_id]),
        )
        self.assertEqual(resolved["status"]["current_state"], "structural_review_accepted")
        self.assertEqual(
            resolved["events"][-1]["event_kind"], "disagreement_resolution"
        )
        self.assertEqual(
            resolved["status"]["unresolved_disagreement_event_ids"], []
        )

    def test_final_rejection_requires_explicit_human_and_not_outcome_valence(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        rejected = self.store.reject_experience_workflow_case(
            workflow_case_id,
            reviewer="Parwa",
            reason_code="source_not_suitable",
            notes="Explicit human rejection.",
        )
        self.assertEqual(rejected["status"]["current_state"], "rejected")
        self.assertFalse(
            rejected["events"][-1]["event_data"]["historical_outcome_valence_used"]
        )
        self.assertEqual(self.store.get_experience_workflow_pool(), [])

    def test_failed_final_acceptance_rolls_back_stage_case_review_and_event(self) -> None:
        workflow_case_id = _draft_and_validate(self.store, self.fixture)
        self.store.review_experience_workflow_structure(
            workflow_case_id,
            decision="accepted",
            reviewer="Parwa",
            review=_structural_review(),
        )
        self.store.review_experience_workflow_outcome(
            workflow_case_id,
            decision="accepted",
            reviewer="Parwa",
            review=_outcome_review(),
        )
        before_events = self.store.get_experience_workflow_events(workflow_case_id)
        outcome_id = self.fixture["outcome"]["outcome_id"]
        with self.store.connect() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT outcome_json FROM resolved_outcomes WHERE outcome_id = ?",
                    (outcome_id,),
                ).fetchone()[0]
            )
            payload["final_reviewed_interpretation"] = "Tampered after review."
            connection.execute(
                "UPDATE resolved_outcomes SET outcome_json = ? WHERE outcome_id = ?",
                (json.dumps(payload, sort_keys=True), outcome_id),
            )
        with self.assertRaises(ValueError):
            self.store.accept_experience_workflow_case(
                workflow_case_id,
                reviewer="Parwa",
                rationale="Must roll back.",
            )
        self.assertEqual(
            self.store.get_experience_workflow_events(workflow_case_id), before_events
        )
        self.assertEqual(self.store.stats()["experience_cases"], 0)
        self.assertEqual(self.store.stats()["experience_reviews"], 0)


class WorkflowSafetyCompatibilityAndCliTests(unittest.TestCase):
    def test_changing_outcome_does_not_change_structural_review_or_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fixture = _persist_fixture(store)
            workflow_case_id = _draft_and_validate(store, fixture)
            store.review_experience_workflow_structure(
                workflow_case_id,
                decision="accepted",
                reviewer="Parwa",
                review=_structural_review(),
            )
            before = store.inspect_experience_workflow_case(workflow_case_id)
            structural_before = deepcopy(before["events"][-1])
            latest = store.get_latest_hypothesis_snapshot(str(fixture["case"]["case_id"]))
            revised = build_reviewed_outcome(
                latest,
                resolved_hypothesis="larger_wave_w_complete",
                final_reviewed_interpretation="Revised historical interpretation.",
                resolution_cutoff=_date(46),
                reviewer="Parwa",
            )
            store.revise_outcome_resolution(revised)
            after = store.inspect_experience_workflow_case(workflow_case_id)
            self.assertEqual(structural_before, after["events"][-1])
            self.assertEqual(
                before["workflow_case"]["content_hash"],
                after["workflow_case"]["content_hash"],
            )

    def test_favorable_or_unfavorable_wording_never_auto_accepts_or_rejects(self) -> None:
        for wording in (
            "Extremely favorable historical movement.",
            "Extremely unfavorable historical movement.",
        ):
            with self.subTest(wording=wording), tempfile.TemporaryDirectory() as directory:
                store = KnowledgeStore(Path(directory) / "brain.sqlite3")
                fixture = _persist_fixture(store)
                outcome = store.get_current_reviewed_outcome(str(fixture["case"]["case_id"]))
                outcome["final_reviewed_interpretation"] = wording
                workflow_case_id = _draft_and_validate(store, fixture)
                self.assertEqual(
                    store.inspect_experience_workflow_case(workflow_case_id)["status"]["current_state"],
                    "ready_for_structural_review",
                )
                self.assertEqual(store.get_experience_workflow_pool(), [])

    def test_duplicate_material_episode_and_explicit_supersession_preserve_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fixture = _persist_fixture(store)
            first = store.create_experience_workflow_draft(
                str(fixture["case"]["case_id"]), recorded_at=_date(50)
            )
            first_id = first["workflow_case"]["workflow_case_id"]
            revised_fingerprint = _fingerprint("NASDAQ:WFLOW", 0, with_rsi=True)
            revised_saved = store.save_wave_fingerprint(revised_fingerprint)
            revised_endpoint = create_initial_snapshot(
                fixture["case"], fingerprint_references=[revised_fingerprint]
            )
            store.save_hypothesis_snapshot(revised_endpoint)
            second = store.create_experience_workflow_draft(
                str(revised_endpoint["snapshot_id"]),
                fingerprint_id=revised_saved["fingerprint_id"],
                supersedes_workflow_case_id=first_id,
                recorded_at=_date(51),
            )
            second_id = second["workflow_case"]["workflow_case_id"]
            self.assertNotEqual(first_id, second_id)
            self.assertIn(
                first_id,
                second["workflow_case"]["duplicate_detection"][
                    "materially_identical_workflow_case_ids"
                ],
            )
            superseded = store.supersede_experience_workflow_case(
                first_id,
                replacement_workflow_case_id=second_id,
                actor_reference="Parwa",
                recorded_at=_date(52),
            )
            self.assertEqual(superseded["status"]["current_state"], "superseded")
            self.assertGreater(len(superseded["events"]), 3)
            self.assertEqual(store.get_experience_workflow_pool(), [])

    def test_first_and_multiple_accepted_cases_are_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            first_fixture = _persist_fixture(store, symbol="NASDAQ:FIRST", offset=0)
            second_fixture = _persist_fixture(store, symbol="NASDAQ:SECOND", offset=100)
            first_id, first = _accept_workflow(store, first_fixture, day=50)
            second_id, second = _accept_workflow(store, second_fixture, day=150)
            self.assertEqual(len(store.get_experience_workflow_pool()), 2)
            self.assertEqual(len(store.get_accepted_experience_pool()), 2)
            for workflow_id, result in ((first_id, first), (second_id, second)):
                self.assertEqual(result["events"][-1]["event_kind"], "final_acceptance")
                self.assertEqual(result["workflow_case"]["workflow_case_id"], workflow_id)

    def test_phase5a_retrieval_and_phase5b_evidence_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            first_fixture = _persist_fixture(store, symbol="NASDAQ:C1", offset=0)
            second_fixture = _persist_fixture(store, symbol="NASDAQ:C2", offset=100)
            _, first = _accept_workflow(store, first_fixture, day=50)
            _, second = _accept_workflow(store, second_fixture, day=150)
            first_experience_id = first["final_acceptance"]["experience_case_id"]
            second_experience_id = second["final_acceptance"]["experience_case_id"]
            retrieved = store.retrieve_experience_analogues(
                first_experience_id,
                candidate_case_ids=[second_experience_id],
            )
            self.assertIn("content_hash", retrieved)
            outcome_audit = store.inspect_experience_outcome(first_experience_id)
            self.assertIn(
                outcome_audit["historical_reviewed_outcome"]["evidence_state"],
                {"accepted", "accepted_with_limitations"},
            )
            before_first = store.get_experience_workflow_events(
                first["workflow_case"]["workflow_case_id"]
            )
            before_second = store.get_experience_workflow_events(
                second["workflow_case"]["workflow_case_id"]
            )
            store.retrieve_experience_analogues(
                first_experience_id,
                candidate_case_ids=[second_experience_id],
            )
            self.assertEqual(
                before_first,
                store.get_experience_workflow_events(
                    first["workflow_case"]["workflow_case_id"]
                ),
            )
            self.assertEqual(
                before_second,
                store.get_experience_workflow_events(
                    second["workflow_case"]["workflow_case_id"]
                ),
            )

    def test_tampered_event_chain_has_no_derived_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fixture = _persist_fixture(store)
            drafted = store.create_experience_workflow_draft(str(fixture["case"]["case_id"]))
            workflow_id = drafted["workflow_case"]["workflow_case_id"]
            event_id = drafted["events"][-1]["event_id"]
            with store.connect() as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT event_json FROM experience_workflow_events WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()[0]
                )
                payload["event_data"]["tampered"] = True
                connection.execute(
                    "UPDATE experience_workflow_events SET event_json = ? WHERE event_id = ?",
                    (json.dumps(payload, sort_keys=True), event_id),
                )
            inspected = store.inspect_experience_workflow_case(workflow_id)
            self.assertFalse(inspected["status"]["valid"])
            self.assertIsNone(inspected["status"]["current_state"])
            self.assertTrue(inspected["status"]["final_acceptance_blocked"])

    def test_foreign_keys_restrict_source_deletion_and_sqlite_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fixture = _persist_fixture(store)
            _draft_and_validate(store, fixture)
            with self.assertRaises(sqlite3.IntegrityError):
                with store.connect() as connection:
                    connection.execute(
                        "DELETE FROM wave_fingerprints WHERE id = ?",
                        (fixture["fingerprint_saved"]["fingerprint_id"],),
                    )
            with store.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unique_sequence_and_parent_constraints_prevent_branching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            fixture = _persist_fixture(store)
            drafted = store.create_experience_workflow_draft(str(fixture["case"]["case_id"]))
            workflow_id = drafted["workflow_case"]["workflow_case_id"]
            last = drafted["events"][-1]
            with self.assertRaises(sqlite3.IntegrityError):
                with store.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO experience_workflow_events(
                            event_id, workflow_case_id, parent_event_id, sequence_number,
                            event_kind, from_state, to_state, actor_reference,
                            workflow_spec_version, event_schema_version, content_hash,
                            event_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "duplicate-sequence",
                            workflow_id,
                            last["event_id"],
                            3,
                            "source_validation",
                            "assembled",
                            "validation_failed",
                            "test",
                            WORKFLOW_SPEC_VERSION,
                            last["event_schema_version"],
                            "a" * 64,
                            "{}",
                            _date(60),
                        ),
                    )

    def test_cli_json_text_explanation_provenance_and_hash_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "brain.sqlite3"
            store = KnowledgeStore(database)
            fixture = _persist_fixture(store)
            json_output = io.StringIO()
            with redirect_stdout(json_output):
                main(
                    [
                        "--workspace",
                        directory,
                        "--database",
                        str(database),
                        "experience",
                        "workflow-spec",
                        "--format",
                        "json",
                    ]
                )
            parsed = json.loads(json_output.getvalue())
            self.assertEqual(parsed["workflow_spec_version"], WORKFLOW_SPEC_VERSION)
            text_output = io.StringIO()
            with redirect_stdout(text_output):
                main(
                    [
                        "--workspace",
                        directory,
                        "--database",
                        str(database),
                        "experience",
                        "workflow-candidates",
                        "--format",
                        "text",
                        "--explain",
                        "--show-provenance",
                        "--show-hashes",
                    ]
                )
            self.assertIn("Historical Experience Workflow Cases", text_output.getvalue())
            self.assertIn(str(fixture["endpoint"]["snapshot_id"]), text_output.getvalue())

    def test_cli_review_file_and_text_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "brain.sqlite3"
            store = KnowledgeStore(database)
            fixture = _persist_fixture(store)
            workflow_id = _draft_and_validate(store, fixture)
            review_file = Path(directory) / "structural-review.json"
            review_file.write_text(
                json.dumps(_structural_review(), indent=2), encoding="utf-8"
            )
            output = io.StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "--workspace",
                        directory,
                        "--database",
                        str(database),
                        "experience",
                        "review-structure",
                        workflow_id,
                        "--decision",
                        "accepted",
                        "--review-file",
                        str(review_file),
                        "--reviewer",
                        "Parwa",
                        "--format",
                        "text",
                        "--explain",
                    ]
                )
            self.assertIn("structural_review_accepted", output.getvalue())


if __name__ == "__main__":
    unittest.main()
