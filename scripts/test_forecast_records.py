from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import elliott_ai
from elliott_ai.agent import ElliottAgent
from elliott_ai.forecast_records import (
    FORECAST_CALCULATION_VERSION,
    FORECAST_LEDGER_MIGRATION_SQL,
    FORECAST_POLICY_VERSION,
    FORECAST_SCHEMA_VERSION,
    AlternativeHypothesis,
    ClaimEvaluationBasis,
    ConfirmationClaim,
    ConfirmationOperator,
    DatasetCutoff,
    DatasetHashScope,
    EvaluationEligibilityStatus,
    EvidenceRuleClass,
    ExpectedCompletionWindow,
    ForecastDirection,
    ForecastEvidence,
    ForecastEvidenceKind,
    ForecastEvidenceStatus,
    ForecastRecord,
    ForecastRecordState,
    InvalidationClaim,
    InvalidationOperator,
    MainHypothesis,
    SameCandleCollisionPolicy,
    TargetClaim,
    build_forecast_record_from_stored_records,
    canonical_forecast_json,
    canonical_sha256,
    forecast_record_content_hash,
    FORECAST_SCHEMA_DEFINITION,
    forecast_schema_definition_hash,
    validate_forecast_record,
)
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.market_scenario import frozen_technical_premise_content_hash


SYMBOL = "NASDAQ:MSFT"
CUTOFF = "2024-01-31T20:00:00Z"


def _create_sources(
    store: KnowledgeStore,
    *,
    incomplete_candle: bool = False,
    resolution_status: str = "final",
) -> tuple[int, int]:
    run_id = store.save_run(
        symbol=SYMBOL,
        provider="packet",
        model=None,
        question="Decision-time technical analysis",
        request={"features": ["volume", "ewo", "macd"]},
        evidence={
            "market_data": [
                {
                    "source": "fixture_msft_daily.json",
                    "timeframe": "daily",
                    "bar_count": 120,
                    "start_date": "2023-08-01T13:30:00Z",
                    "end_date": CUTOFF,
                    "snapshot": {
                        "source_mode": "offline_fixture",
                        "requested_symbol": SYMBOL,
                        "resolved_symbol": SYMBOL,
                        "listed_exchange": "NASDAQ",
                        "session": "regular",
                        "chart_timezone": "Etc/UTC",
                        "adjustment_basis": {"splits_adjusted": True},
                        "incomplete_candle": incomplete_candle,
                    },
                }
            ]
        },
        response={"analysis_status": "complete", "data_cutoff": CUTOFF},
        validation_errors=[],
    )
    resolution_id = store.save_degree_resolution(
        run_id=run_id,
        provider="openai",
        model="fixture-model",
        request={"mode": "degree_resolution", "source_run_id": run_id},
        response={
            "resolution_status": resolution_status,
            "symbol": SYMBOL,
            "data_cutoff": CUTOFF,
            "scope": "Fixture technical resolution.",
            "degree_hierarchy": [
                {
                    "wave_id": "W1",
                    "degree": "Primary",
                    "wave": "3",
                    "direction": "up",
                    "structure": "impulse",
                    "completion_status": "active",
                }
            ],
            "active_position": {
                "summary": "Primary wave 3 active",
                "wave_ids": ["W1"],
                "confirmation": "Daily close above 420 confirms continuation.",
                "invalidation": "Move below 350 invalidates this count.",
            },
            "alternate_counts": [
                {
                    "description": "Primary wave 1 may still be extending.",
                    "trigger": "A five-wave extension becomes visible.",
                    "invalidation": "A structural overlap invalidates it.",
                }
            ],
            "confirmations": ["Price structure is motive at the stored cutoff."],
            "invalidation_levels": ["Below 350"],
            "unresolved_items": ["Lower-degree subdivision is incomplete."],
            "confidence": {"structure": 0.66, "degree": 0.58},
        },
        validation_errors=[],
        readiness={
            "final_report_ready": resolution_status == "final",
            "blockers": [],
            "warnings": ["Volume comparison is unavailable across feeds."],
        },
    )
    return run_id, resolution_id


def _typed_record(
    store: KnowledgeStore,
    run_id: int,
    resolution_id: int,
    *,
    forecast_version: int = 1,
    supersedes_forecast_id: str | None = None,
    target_high: float = 460.0,
) -> ForecastRecord:
    evidence = (
        ForecastEvidence(
            evidence_id="e-price",
            hypothesis_ids=("main",),
            kind=ForecastEvidenceKind.PRICE_STRUCTURE,
            status=ForecastEvidenceStatus.SUPPORTING,
            statement="The stored child structure supports an upward motive count.",
            rule_class=EvidenceRuleClass.HARD_STRUCTURAL,
            source_wave_ids=("W1",),
        ),
        ForecastEvidence(
            evidence_id="e-rsi",
            hypothesis_ids=("main",),
            kind=ForecastEvidenceKind.RSI,
            status=ForecastEvidenceStatus.UNAVAILABLE,
            statement="RSI was not enabled for this decision-time dataset.",
            reason="RSI is optional and was disabled.",
        ),
    )
    main = MainHypothesis(
        hypothesis_id="main",
        label="Primary wave 3 continuation",
        direction=ForecastDirection.UP,
        degree="Primary",
        wave_label="3",
        pattern_family="impulse",
        completion_status="active",
        summary="The main count expects the active motive wave to continue.",
        count={"wave_ids": ["W1"], "parent": "Cycle III"},
        target_claim_ids=("target-main",),
        invalidation_claim_ids=("invalidate-main",),
        confirmation_claim_ids=("confirm-main",),
        completion_window_ids=("window-main",),
        evidence_ids=("e-price", "e-rsi"),
        original_uncalibrated_confidence={"structure": 0.66, "degree": 0.58},
    )
    alternate = AlternativeHypothesis(
        hypothesis_id="alternate-extension",
        label="Primary wave 1 extension",
        direction=ForecastDirection.UP,
        degree="Primary",
        wave_label="1",
        pattern_family="extended_impulse",
        completion_status="active",
        summary="The prior wave may still be extending.",
        count={"wave_ids": ["A1"]},
        activation_conditions=("A valid lower-degree extension appears.",),
        distinguishing_evidence_needed=("Resolve the lower-degree overlap.",),
        original_uncalibrated_confidence={"structure": 0.31},
    )
    target = TargetClaim(
        claim_id="target-main",
        hypothesis_id="main",
        target_low=440.0,
        target_high=target_high,
        direction=ForecastDirection.UP,
        timeframe="daily",
        price_basis="ohlc",
        rationale="A stored Fibonacci extension cluster.",
        evidence_ids=("e-price",),
        source_wave_ids=("W1",),
    )
    invalidation = InvalidationClaim(
        claim_id="invalidate-main",
        hypothesis_id="main",
        operator=InvalidationOperator.AT_OR_BELOW,
        condition="Price touches or breaches 350.",
        timeframe="daily",
        price_basis="ohlc",
        price_level=350.0,
        evidence_ids=("e-price",),
        source_wave_ids=("W1",),
    )
    confirmation = ConfirmationClaim(
        claim_id="confirm-main",
        hypothesis_id="main",
        operator=ConfirmationOperator.AT_OR_ABOVE,
        condition="A completed daily candle closes at or above 420.",
        timeframe="daily",
        price_basis="close",
        price_level=420.0,
        evidence_ids=("e-price",),
        source_wave_ids=("W1",),
    )
    window = ExpectedCompletionWindow(
        window_id="window-main",
        hypothesis_id="main",
        start_utc="2024-02-01T00:00:00Z",
        end_utc="2024-04-30T23:59:59Z",
        timeframe="daily",
        start_rule="First completed candle after the analysis cutoff.",
        end_rule="Last completed candle within the forecast window.",
        rationale="The stored degree-duration estimate.",
    )
    return build_forecast_record_from_stored_records(
        store.get_run(run_id),
        store.get_degree_resolution(resolution_id),
        main_hypothesis=main,
        alternative_hypotheses=(alternate,),
        target_claims=(target,),
        invalidation_claims=(invalidation,),
        confirmation_claims=(confirmation,),
        expected_completion_windows=(window,),
        evidence=evidence,
        forecast_version=forecast_version,
        supersedes_forecast_id=supersedes_forecast_id,
    )


class ForecastContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = KnowledgeStore(Path(self.temporary.name) / "brain.sqlite3")
        self.run_id, self.resolution_id = _create_sources(self.store)
        self.record = _typed_record(self.store, self.run_id, self.resolution_id)

    def test_contracts_and_nested_json_are_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.record.symbol = "OTHER"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            self.record.provider_versions["analysis"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            self.record.main_hypothesis.count["parent"] = "changed"  # type: ignore[index]
        self.assertIsInstance(self.record.dataset_cutoffs, tuple)
        self.assertIsInstance(self.record.alternative_hypotheses, tuple)

    def test_claim_defaults_are_explicit(self) -> None:
        self.assertIs(
            self.record.target_claims[0].evaluation_basis,
            ClaimEvaluationBasis.INTRABAR_TOUCH,
        )
        self.assertIs(
            self.record.invalidation_claims[0].evaluation_basis,
            ClaimEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
        )
        self.assertIs(
            self.record.confirmation_claims[0].evaluation_basis,
            ClaimEvaluationBasis.CANDLE_CLOSE,
        )
        self.assertIs(
            self.record.same_candle_collision_policy,
            SameCandleCollisionPolicy.INCOMPARABLE_WITHOUT_LOWER_TIMEFRAME_ORDER,
        )

    def test_canonical_serialization_ignores_mapping_insertion_order(self) -> None:
        payload = self.record.to_dict()
        payload["provider_versions"] = dict(
            reversed(list(payload["provider_versions"].items()))
        )
        payload["content_hash"] = forecast_record_content_hash(payload)
        restored = ForecastRecord.from_dict(payload)
        self.assertEqual(restored.content_hash, self.record.content_hash)
        self.assertEqual(
            canonical_forecast_json(restored), canonical_forecast_json(self.record)
        )

    def test_repeated_build_is_deterministic(self) -> None:
        second = _typed_record(self.store, self.run_id, self.resolution_id)
        self.assertEqual(second.forecast_id, self.record.forecast_id)
        self.assertEqual(second.content_hash, self.record.content_hash)

    def test_top_level_tampering_is_detected(self) -> None:
        payload = self.record.to_dict()
        payload["symbol"] = "NASDAQ:TSLA"
        with self.assertRaisesRegex(ValueError, "content_hash"):
            ForecastRecord.from_dict(payload)

    def test_nested_premise_tampering_is_detected_even_if_outer_hash_is_rebuilt(self) -> None:
        payload = self.record.to_dict()
        premise = payload["frozen_technical_premise"]
        premise["technical_structure"]["degree_resolution"]["scope"] = "tampered"
        payload["content_hash"] = forecast_record_content_hash(payload)
        with self.assertRaisesRegex(ValueError, "Frozen technical premise hash"):
            ForecastRecord.from_dict(payload)

    def test_fundamentals_cannot_enter_frozen_technical_premise(self) -> None:
        payload = self.record.to_dict()
        premise = payload["frozen_technical_premise"]
        premise["technical_structure"]["fundamentals"] = {"revenue": 1}
        premise["frozen_content_hash"] = frozen_technical_premise_content_hash(
            premise
        )
        payload["content_hash"] = forecast_record_content_hash(payload)
        with self.assertRaisesRegex(ValueError, "post-technical context"):
            ForecastRecord.from_dict(payload)

    def test_dataset_after_analysis_cutoff_is_rejected(self) -> None:
        payload = self.record.to_dict()
        payload["dataset_cutoffs"][0]["cutoff_utc"] = "2024-02-01T00:00:00Z"
        payload["content_hash"] = forecast_record_content_hash(payload)
        with self.assertRaisesRegex(ValueError, "extends beyond"):
            ForecastRecord.from_dict(payload)

    def test_naive_dataset_cutoff_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            DatasetCutoff(
                dataset_id="d1",
                source_reference="fixture.json",
                timeframe="daily",
                cutoff_utc="2024-01-01T12:00:00",
                dataset_hash="a" * 64,
                hash_scope=DatasetHashScope.STORED_DATASET_SUMMARY,
                provider="fixture",
                feed_identity="fixture|daily",
                requested_symbol=SYMBOL,
                resolved_symbol=SYMBOL,
                exchange="NASDAQ",
                session="regular",
                timezone="UTC",
                adjustment="splits",
                price_basis="ohlc",
            )

    def test_main_and_alternative_hypotheses_remain_separate(self) -> None:
        self.assertIsInstance(self.record.main_hypothesis, MainHypothesis)
        self.assertEqual(self.record.main_hypothesis.hypothesis_id, "main")
        self.assertEqual(len(self.record.alternative_hypotheses), 1)
        self.assertIsInstance(
            self.record.alternative_hypotheses[0], AlternativeHypothesis
        )
        self.assertNotEqual(
            self.record.main_hypothesis.count,
            self.record.alternative_hypotheses[0].count,
        )

    def test_invalid_target_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "target_low"):
            TargetClaim(
                claim_id="bad",
                hypothesis_id="main",
                target_low=20,
                target_high=10,
                direction=ForecastDirection.UP,
                timeframe="daily",
                price_basis="ohlc",
                rationale="Invalid range.",
            )

    def test_dangling_claim_reference_is_rejected(self) -> None:
        payload = self.record.to_dict()
        payload["main_hypothesis"]["target_claim_ids"].append("missing")
        payload["content_hash"] = forecast_record_content_hash(payload)
        with self.assertRaisesRegex(ValueError, "unknown claim"):
            ForecastRecord.from_dict(payload)

    def test_missing_rsi_remains_unavailable_soft_evidence(self) -> None:
        item = self.record.unavailable_evidence[0]
        self.assertIs(item.kind, ForecastEvidenceKind.RSI)
        self.assertIs(item.rule_class, EvidenceRuleClass.SOFT_TECHNICAL)
        with self.assertRaisesRegex(ValueError, "soft"):
            ForecastEvidence(
                evidence_id="bad-rsi",
                hypothesis_ids=("main",),
                kind=ForecastEvidenceKind.RSI,
                status=ForecastEvidenceStatus.CONTRADICTORY,
                statement="RSI differs.",
                rule_class=EvidenceRuleClass.HARD_STRUCTURAL,
            )

    def test_price_and_timing_eligibility_are_separate(self) -> None:
        eligibility = self.record.evaluation_eligibility
        self.assertTrue(eligibility.price_accuracy_evaluable)
        self.assertTrue(eligibility.timing_accuracy_evaluable)
        self.assertIs(eligibility.status, EvaluationEligibilityStatus.EVALUABLE)

    def test_incomplete_candle_marks_record_provisional(self) -> None:
        run_id, resolution_id = _create_sources(
            self.store, incomplete_candle=True
        )
        record = _typed_record(self.store, run_id, resolution_id)
        self.assertFalse(record.dataset_cutoffs[0].completed_candles_only)
        self.assertIs(
            record.evaluation_eligibility.record_state,
            ForecastRecordState.PROVISIONAL,
        )
        self.assertIn(
            "incomplete_candle", record.evaluation_eligibility.reason_codes
        )

    def test_completed_candles_default_to_final(self) -> None:
        self.assertTrue(self.record.dataset_cutoffs[0].completed_candles_only)
        self.assertIs(
            self.record.evaluation_eligibility.record_state,
            ForecastRecordState.FINAL,
        )

    def test_legacy_adapter_does_not_reconstruct_narrative_claims(self) -> None:
        legacy = build_forecast_record_from_stored_records(
            self.store.get_run(self.run_id),
            self.store.get_degree_resolution(self.resolution_id),
        )
        self.assertIs(
            legacy.evaluation_eligibility.status,
            EvaluationEligibilityStatus.LEGACY_UNSCORABLE,
        )
        self.assertFalse(legacy.evaluation_eligibility.typed_claims_present)
        self.assertEqual(legacy.target_claims, ())
        self.assertEqual(legacy.invalidation_claims, ())
        self.assertEqual(legacy.confirmation_claims, ())
        self.assertEqual(legacy.expected_completion_windows, ())
        self.assertEqual(
            legacy.frozen_technical_premise.technical_structure[
                "degree_resolution"
            ]["invalidation_levels"],
            ("Below 350",),
        )

    def test_schema_and_policy_versions_are_preserved(self) -> None:
        self.assertEqual(self.record.schema_version, FORECAST_SCHEMA_VERSION)
        self.assertEqual(
            self.record.calculation_version, FORECAST_CALCULATION_VERSION
        )
        self.assertEqual(self.record.policy_version, FORECAST_POLICY_VERSION)
        self.assertRegex(self.record.content_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(forecast_schema_definition_hash(), r"^[0-9a-f]{64}$")
        with self.assertRaises(TypeError):
            FORECAST_SCHEMA_DEFINITION["policies"]["target_default"] = "changed"  # type: ignore[index]


class ForecastPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.run_id, self.resolution_id = _create_sources(self.store)
        self.record = _typed_record(self.store, self.run_id, self.resolution_id)

    def test_migration_adds_only_the_two_forecast_tables(self) -> None:
        with self.store.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('forecast_schema_versions', 'forecast_records')"
                )
            }
            version = connection.execute(
                "SELECT * FROM forecast_schema_versions"
            ).fetchone()
        self.assertEqual(tables, {"forecast_schema_versions", "forecast_records"})
        self.assertEqual(version["schema_version"], FORECAST_SCHEMA_VERSION)
        self.assertEqual(version["content_hash"], forecast_schema_definition_hash())

    def test_migration_rolls_back_all_phase11_objects_on_failure(self) -> None:
        database = Path(self.temporary.name) / "rollback.sqlite3"
        broken_sql = (
            FORECAST_LEDGER_MIGRATION_SQL[0],
            "CREATE TABLE deliberately_broken(",
        )
        with patch(
            "elliott_ai.knowledge.FORECAST_LEDGER_MIGRATION_SQL", broken_sql
        ):
            with self.assertRaises(sqlite3.OperationalError):
                KnowledgeStore(database)
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertNotIn("forecast_schema_versions", tables)
        self.assertNotIn("forecast_records", tables)

    def test_create_get_list_and_idempotent_round_trip(self) -> None:
        first = self.store.create_forecast_record(self.record)
        second = self.store.create_forecast_record(self.record)
        restored = self.store.get_forecast_record(self.record.forecast_id)
        listed = self.store.list_forecast_records(symbol=SYMBOL)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(restored, self.record)
        self.assertEqual(listed, [self.record])

    def test_source_hash_drift_blocks_persistence(self) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE analysis_runs SET question = ? WHERE id = ?",
                ("Changed after forecast construction", self.run_id),
            )
        with self.assertRaisesRegex(ValueError, "source hash"):
            self.store.create_forecast_record(self.record)

    def test_source_hash_drift_after_insertion_is_detected_on_read(self) -> None:
        self.store.create_forecast_record(self.record)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE analysis_runs SET question = ? WHERE id = ?",
                ("Changed after ledger insertion", self.run_id),
            )
        with self.assertRaisesRegex(RuntimeError, "source-chain validation"):
            self.store.get_forecast_record(self.record.forecast_id)

    def test_database_triggers_reject_update_and_delete(self) -> None:
        self.store.create_forecast_record(self.record)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE forecast_records SET symbol='OTHER' WHERE forecast_id=?",
                    (self.record.forecast_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM forecast_records WHERE forecast_id=?",
                    (self.record.forecast_id,),
                )

    def test_database_payload_tampering_is_detected_on_read(self) -> None:
        self.store.create_forecast_record(self.record)
        with self.store.connect() as connection:
            connection.execute("DROP TRIGGER forecast_records_no_update")
            payload = self.record.to_dict()
            payload["symbol"] = "NASDAQ:TSLA"
            connection.execute(
                "UPDATE forecast_records SET record_json=? WHERE forecast_id=?",
                (json.dumps(payload), self.record.forecast_id),
            )
        with self.assertRaisesRegex(RuntimeError, "immutable validation"):
            self.store.get_forecast_record(self.record.forecast_id)

    def test_foreign_keys_restrict_source_deletion(self) -> None:
        self.store.create_forecast_record(self.record)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM degree_resolutions WHERE id = ?",
                    (self.resolution_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "DELETE FROM analysis_runs WHERE id = ?", (self.run_id,)
                )
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_explicit_linear_supersession_preserves_both_records(self) -> None:
        self.store.create_forecast_record(self.record)
        successor = _typed_record(
            self.store,
            self.run_id,
            self.resolution_id,
            forecast_version=2,
            supersedes_forecast_id=self.record.forecast_id,
            target_high=470.0,
        )
        self.store.create_forecast_record(successor)
        all_records = self.store.list_forecast_records(symbol=SYMBOL)
        current = self.store.list_forecast_records(
            symbol=SYMBOL, include_superseded=False
        )
        self.assertEqual({item.forecast_id for item in all_records}, {
            self.record.forecast_id,
            successor.forecast_id,
        })
        self.assertEqual(current, [successor])
        branch = _typed_record(
            self.store,
            self.run_id,
            self.resolution_id,
            forecast_version=2,
            supersedes_forecast_id=self.record.forecast_id,
            target_high=480.0,
        )
        with self.assertRaisesRegex(
            ValueError, "already exists|already has"
        ):
            self.store.create_forecast_record(branch)

    def test_invalid_supersession_version_is_rejected_by_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "without a predecessor"):
            _typed_record(
                self.store,
                self.run_id,
                self.resolution_id,
                forecast_version=2,
            )

    def test_no_update_or_delete_persistence_methods_exist(self) -> None:
        self.assertFalse(hasattr(self.store, "update_forecast_record"))
        self.assertFalse(hasattr(self.store, "delete_forecast_record"))

    def test_legacy_database_is_readable_and_not_backfilled(self) -> None:
        database = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE analysis_runs (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                question TEXT NOT NULL,
                request_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                validation_json TEXT NOT NULL
            );
            CREATE TABLE degree_resolutions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                readiness_json TEXT NOT NULL
            );
            INSERT INTO analysis_runs VALUES (
                1, '2024-02-01T00:00:00+00:00', 'NASDAQ:MSFT', 'packet', NULL,
                'legacy', '{}', '{}', '{}', '[]'
            );
            INSERT INTO degree_resolutions VALUES (
                1, 1, '2024-02-01T00:00:01+00:00', 'packet', NULL,
                '{}', '{}', '[]', '{}'
            );
            """
        )
        connection.commit()
        connection.close()
        migrated = KnowledgeStore(database)
        self.assertIsNotNone(migrated.forecast_ledger_backup_path)
        self.assertEqual(migrated.get_run(1)["question"], "legacy")
        self.assertEqual(migrated.get_degree_resolution(1)["run_id"], 1)
        self.assertEqual(migrated.list_forecast_records(), [])

    def test_existing_public_analysis_interfaces_are_unchanged(self) -> None:
        self.assertEqual(
            elliott_ai.__all__, ["AnalysisRequest", "ElliottAgent", "KnowledgeStore"]
        )
        self.assertEqual(
            list(inspect.signature(ElliottAgent.analyze).parameters),
            ["self", "request"],
        )
        self.assertEqual(
            list(inspect.signature(ElliottAgent.resolve_degrees).parameters),
            ["self", "source_run_id", "question"],
        )


class ForecastValidationFunctionTests(unittest.TestCase):
    def test_validation_reports_hash_mismatch_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            run_id, resolution_id = _create_sources(store)
            record = _typed_record(store, run_id, resolution_id)
            payload = record.to_dict()
            payload["content_hash"] = "0" * 64
            errors = validate_forecast_record(payload)
            self.assertTrue(any("content_hash" in item for item in errors))
            self.assertEqual(record.content_hash, forecast_record_content_hash(record))

    def test_canonical_sha_rejects_non_finite_values(self) -> None:
        with self.assertRaises(ValueError):
            canonical_sha256({"bad": float("nan")})


if __name__ == "__main__":
    unittest.main()
