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
    CORRECTION_STATE_CALCULATION_VERSION,
    CORRECTION_STATE_SCHEMA_VERSION,
    EVIDENCE_TIMING_CLASSES,
    OUTCOME_SCHEMA_VERSION,
    advance_correction_snapshot,
    build_reviewed_outcome,
    create_correction_case,
    create_initial_snapshot,
    create_review_snapshot,
    detect_post_terminal_events,
    snapshot_content_hash,
    validate_snapshot,
    with_evidence_timing,
)
from elliott_ai.evidence import make_evidence_item
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.reporting import render_correction_lineage_report


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _date(day: int) -> str:
    return (START + timedelta(days=day)).date().isoformat()


def _candles(prices: list[float], *, start_day: int = 0) -> list[dict[str, object]]:
    return [
        {
            "date": _date(start_day + index),
            "open": price - 0.25,
            "high": price + 0.75,
            "low": price - 0.75,
            "close": price,
            "volume": 1_000_000 + index * 10_000,
        }
        for index, price in enumerate(prices)
    ]


def _case(
    *,
    symbol: str = "NASDAQ:TEST",
    label: str = "Y",
    family: str = "double-three",
    endpoint_day: int = 0,
    price: float = 100.0,
    invalidation_levels: dict[str, object] | None = None,
) -> dict[str, object]:
    return create_correction_case(
        symbol=symbol,
        timeframe="daily",
        candidate_endpoint={
            "date": _date(endpoint_day),
            "price": price,
            "terminal_label": label,
            "terminal_direction": "down",
            "protected_origin": {"date": _date(endpoint_day), "price": price},
        },
        elliott_degree="Minor",
        parent_pattern_family=family,
        source_identity={
            "provider": "tradingview",
            "venue": "NASDAQ",
            "market_type": "equity",
            "feed_identity": f"tv|{symbol}|daily|regular",
        },
        explicit_price_invalidation_levels=invalidation_levels,
    )


def _state(snapshot: dict[str, object], hypothesis_id: str) -> dict[str, object]:
    return next(
        item
        for item in snapshot["hypothesis_states"]
        if item["hypothesis_id"] == hypothesis_id
    )


def _indicator(
    evidence_type: str,
    status: str,
    *,
    feature: str | None = None,
) -> dict[str, object]:
    return make_evidence_item(
        evidence_type=evidence_type,
        feature_name=feature or f"{evidence_type}.terminal_observation",
        observed_value=True,
        status=status,
        reason=f"Synthetic {evidence_type} observation.",
    )


def _five_wave_context(*, with_retracement: bool = False, with_motive: bool = False) -> dict[str, object]:
    pivot_prices = [100.0, 110.0, 104.0, 122.0, 113.0, 128.0]
    pivots = [
        {"date": _date(index + 1), "price": price}
        for index, price in enumerate(pivot_prices)
    ]
    result: dict[str, object] = {
        "five_wave_move_away": {
            "pivots": pivots,
            "direction": "up",
            "pattern_family": "impulse",
            "hard_rules_valid": True,
        }
    }
    if with_retracement:
        result["retracement"] = {
            "observed": True,
            "observed_at": _date(9),
            "structure": "zigzag",
            "child_count": 3,
            "pivots": [128.0, 121.0, 124.0, 117.0],
        }
    if with_motive:
        result["subsequent_motive_candidate"] = {
            "observed": True,
            "observed_at": _date(11),
            "reason": "A later five-child motive candidate accelerated upward.",
            "hard_rules_valid": True,
            "pivots": [
                {"date": _date(8), "price": 117.0},
                {"date": _date(9), "price": 124.0},
                {"date": _date(10), "price": 120.0},
                {"date": _date(11), "price": 132.0},
            ],
        }
    return result


def _resolved_outcome(
    snapshot: dict[str, object], hypothesis_id: str, *, reviewer: str = "human-reviewer"
) -> dict[str, object]:
    return build_reviewed_outcome(
        snapshot,
        resolved_hypothesis=hypothesis_id,
        final_reviewed_interpretation=f"Reviewed as {hypothesis_id}.",
        resolution_cutoff=_date(30),
        reviewer=reviewer,
        resolution_confidence="high_after_human_review",
        supporting_future_structure=["Later child structure resolved the ambiguity."],
        rejected_alternatives=[
            {
                "hypothesis_id": "correction_still_in_progress",
                "reason": "Later structure completed without another same-degree corrective leg.",
            }
        ],
        notes="Human-reviewed fixture; not a model probability.",
    )


class DecisionTimeSnapshotTests(unittest.TestCase):
    def test_endpoint_snapshot_excludes_post_terminal_evidence(self) -> None:
        case = _case()
        later = with_evidence_timing(
            _indicator("rsi", "supportive"),
            timing_class="available_after_endpoint",
            available_at=_date(2),
        )
        endpoint = create_initial_snapshot(case, evidence_items=[later])
        self.assertEqual(endpoint["evidence_catalog"], [])
        self.assertEqual(endpoint["timeline"]["first_displacement_confirmation_time"], None)

    def test_later_candles_cannot_mutate_old_snapshot(self) -> None:
        endpoint = create_initial_snapshot(_case())
        frozen = deepcopy(endpoint)
        advance_correction_snapshot(
            endpoint,
            cutoff=_date(5),
            newly_available_candles=_candles([100, 103, 106, 109, 112, 115]),
        )
        self.assertEqual(endpoint, frozen)
        self.assertEqual(endpoint["content_hash"], snapshot_content_hash(endpoint))

    def test_extreme_values_after_cutoff_do_not_change_prior_child_snapshot(self) -> None:
        endpoint = create_initial_snapshot(_case())
        normal = _candles([100, 102, 104, 106, 108, 110, 112, 114])
        attacked = deepcopy(normal)
        for row in attacked[5:]:
            row["high"] = 9_000_000.0
            row["low"] = -9_000_000.0
            row["close"] = 8_000_000.0
        first = advance_correction_snapshot(
            endpoint, cutoff=_date(4), newly_available_candles=normal
        )
        second = advance_correction_snapshot(
            endpoint, cutoff=_date(4), newly_available_candles=attacked
        )
        self.assertEqual(first, second)

    def test_new_cutoff_creates_linked_snapshot_instead_of_overwrite(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(endpoint, cutoff=_date(3))
        self.assertNotEqual(endpoint["snapshot_id"], child["snapshot_id"])
        self.assertEqual(child["parent_snapshot_id"], endpoint["snapshot_id"])
        self.assertEqual(endpoint["parent_snapshot_id"], None)

    def test_original_fingerprint_hashes_are_referenced(self) -> None:
        case = _case()
        fingerprint = {
            "content_hash": "a" * 64,
            "identity_hash": "b" * 64,
            "feature_schema_version": "wave-fingerprint-1.0.0",
            "calculation_version": "wave-fingerprint-calc-1.0.0",
            "cutoff": {"timestamp": _date(0)},
            "identity": {"wave_id": "Y"},
        }
        endpoint = create_initial_snapshot(case, fingerprint_references=[fingerprint])
        self.assertEqual(endpoint["fingerprint_references"][0]["content_hash"], "a" * 64)

    def test_all_evidence_timing_classes_are_explicit(self) -> None:
        self.assertEqual(
            EVIDENCE_TIMING_CLASSES,
            {
                "available_at_endpoint",
                "available_after_endpoint",
                "available_at_resolution",
                "human_review_only",
            },
        )

    def test_repeated_generation_is_deterministic(self) -> None:
        case = _case()
        first = create_initial_snapshot(case)
        second = create_initial_snapshot(case)
        self.assertEqual(first, second)
        self.assertEqual(validate_snapshot(first), [])


class CorrectionScenarioTests(unittest.TestCase):
    def test_completed_wxy_preserves_all_contextually_valid_roles(self) -> None:
        snapshot = create_initial_snapshot(_case())
        expected = {
            "correction_complete",
            "larger_wave_w_complete",
            "larger_wave_a_complete",
            "triple_three_continuation",
            "local_wave_y_complete",
        }
        self.assertTrue(expected.issubset(set(snapshot["active_hypotheses"])))

    def test_structurally_complete_c_without_reversal_remains_provisional(self) -> None:
        snapshot = create_initial_snapshot(_case(label="C", family="zigzag"))
        self.assertEqual(_state(snapshot, "local_wave_c_complete")["state"], "structurally_complete")
        self.assertTrue(snapshot["provisional"])
        self.assertNotEqual(_state(snapshot, "correction_complete")["state"], "structurally_confirmed")

    def test_structural_completion_event_is_not_whole_correction_confirmation(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(
            endpoint,
            cutoff=_date(1),
            structural_context={
                "structural_completion": {
                    "observed": True,
                    "observed_at": _date(1),
                    "reason": "The final local child pivot was confirmed.",
                }
            },
        )
        self.assertEqual(
            _state(child, "correction_complete")["state"],
            "terminal_evidence_supportive",
        )
        self.assertEqual(
            child["timeline"]["first_structural_completion_time"],
            f"{_date(0)}T00:00:00+00:00",
        )
        transition = next(
            item
            for item in child["transitions"]
            if item["hypothesis_id"] == "correction_complete"
        )
        self.assertEqual(
            transition["analysis_cutoff"], f"{_date(1)}T00:00:00+00:00"
        )
        self.assertTrue(child["provisional"])

    def test_rsi_divergence_without_price_confirmation_is_only_supportive(self) -> None:
        evidence = _indicator("rsi", "supportive", feature="rsi.bullish_divergence_candidate")
        snapshot = create_initial_snapshot(_case(), evidence_items=[evidence])
        self.assertEqual(snapshot["evidence_references"]["supporting"].__len__(), 1)
        self.assertTrue(snapshot["provisional"])
        self.assertFalse(any(item["state"] == "structurally_confirmed" for item in snapshot["hypothesis_states"]))

    def test_strong_volume_without_structure_does_not_resolve(self) -> None:
        snapshot = create_initial_snapshot(
            _case(), evidence_items=[_indicator("volume", "supportive")]
        )
        self.assertTrue(snapshot["provisional"])
        self.assertIn("correction_complete", snapshot["active_hypotheses"])

    def test_indicator_basis_cannot_remove_structural_candidate(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(
            endpoint,
            cutoff=_date(2),
            structural_invalidations=[
                {
                    "hypothesis_id": "correction_complete",
                    "basis": "rsi",
                    "reason": "RSI did not diverge.",
                }
            ],
        )
        self.assertIn("correction_complete", child["active_hypotheses"])
        self.assertTrue(child["ignored_nonstructural_invalidations"])

    def test_explicit_structural_invalidation_removes_candidate(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(
            endpoint,
            cutoff=_date(2),
            structural_invalidations=[
                {
                    "hypothesis_id": "larger_wave_w_complete",
                    "basis": "hard_structural_rule",
                    "reason": "Later children cannot form the required larger X-Y sequence.",
                    "hard_structural_rule": "required child sequence impossible",
                }
            ],
        )
        self.assertIn("larger_wave_w_complete", child["invalidated_hypotheses"])
        self.assertNotIn("larger_wave_w_complete", child["active_hypotheses"])

    def test_explicit_price_level_is_evaluated_against_new_candles(self) -> None:
        endpoint = create_initial_snapshot(
            _case(
                invalidation_levels={
                    "larger_wave_w_complete": {
                        "price": 98.0,
                        "invalidated_when": "below",
                    }
                }
            )
        )
        rows = _candles([100.0, 101.0, 97.5])
        rows[-1]["low"] = 97.0
        child = advance_correction_snapshot(
            endpoint, cutoff=_date(2), newly_available_candles=rows
        )
        self.assertIn("larger_wave_w_complete", child["invalidated_hypotheses"])
        transition = next(
            item
            for item in child["transitions"]
            if item["hypothesis_id"] == "larger_wave_w_complete"
        )
        self.assertEqual(transition["price_invalidation_level"], 98.0)

    def test_unavailable_lower_timeframe_keeps_five_wave_status_unavailable(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(endpoint, cutoff=_date(4))
        event = child["post_terminal_events"]["five_wave_move_away"]
        self.assertEqual(event["status"], "unavailable")
        self.assertNotIn("new_motive_wave_1", child["active_hypotheses"])

    def test_directional_move_without_pivots_is_only_candidate(self) -> None:
        events = detect_post_terminal_events(
            candidate_endpoint=_case()["candidate_endpoint"],
            newly_available_candles=_candles([100, 105, 110]),
            cutoff=_date(2),
            structural_context={"five_wave_move_away": {"observed": True}},
        )
        self.assertEqual(events["five_wave_move_away"]["status"], "candidate")
        self.assertTrue(events["five_wave_move_away"]["provisional"])

    def test_unverified_directional_move_does_not_create_wave_1_or_wave_a(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(
            endpoint,
            cutoff=_date(2),
            newly_available_candles=_candles([100, 105, 110]),
            structural_context={"five_wave_move_away": {"observed": True}},
        )
        self.assertNotIn("new_motive_wave_1", child["active_hypotheses"])
        self.assertNotIn("corrective_wave_a", child["active_hypotheses"])

    def test_five_waves_away_preserves_wave_1_and_wave_a(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(
            endpoint,
            cutoff=_date(6),
            newly_available_candles=_candles([100, 110, 104, 122, 113, 128, 127]),
            structural_context=_five_wave_context(),
        )
        self.assertIn("new_motive_wave_1", child["active_hypotheses"])
        self.assertIn("corrective_wave_a", child["active_hypotheses"])
        self.assertEqual(_state(child, "new_motive_wave_1")["state"], "five_wave_move_away_candidate")

    def test_corrective_retracement_preserves_wave_1_and_wave_a(self) -> None:
        endpoint = create_initial_snapshot(_case())
        five = advance_correction_snapshot(
            endpoint,
            cutoff=_date(6),
            newly_available_candles=_candles([100, 110, 104, 122, 113, 128, 127]),
            structural_context=_five_wave_context(),
        )
        retraced = advance_correction_snapshot(
            five,
            cutoff=_date(9),
            newly_available_candles=_candles([100, 110, 104, 122, 113, 128, 127, 123, 120, 117]),
            structural_context=_five_wave_context(with_retracement=True),
        )
        self.assertIn("new_motive_wave_1", retraced["active_hypotheses"])
        self.assertIn("corrective_wave_a", retraced["active_hypotheses"])
        self.assertGreaterEqual(
            retraced["timeline"]["retracement_confirmation_time"], _date(9)
        )

    def test_subsequent_motive_acceleration_advances_wave_1_only(self) -> None:
        endpoint = create_initial_snapshot(_case())
        child = advance_correction_snapshot(
            endpoint,
            cutoff=_date(11),
            newly_available_candles=_candles([100, 110, 104, 122, 113, 128, 125, 121, 118, 117, 124, 132]),
            structural_context=_five_wave_context(with_retracement=True, with_motive=True),
        )
        self.assertEqual(_state(child, "new_motive_wave_1")["state"], "next_motive_leg_candidate")
        self.assertNotEqual(_state(child, "corrective_wave_a")["state"], "invalidated")

    def test_return_through_origin_invalidates_new_trend_hypothesis(self) -> None:
        endpoint = create_initial_snapshot(_case())
        five = advance_correction_snapshot(
            endpoint,
            cutoff=_date(6),
            newly_available_candles=_candles([100, 110, 104, 122, 113, 128, 127]),
            structural_context=_five_wave_context(),
        )
        rows = _candles([100, 110, 104, 122, 113, 128, 127, 105, 99])
        rows[-1]["low"] = 98.0
        failed = advance_correction_snapshot(
            five,
            cutoff=_date(8),
            newly_available_candles=rows,
            structural_context=_five_wave_context(),
        )
        self.assertIn("new_motive_wave_1", failed["invalidated_hypotheses"])
        self.assertIn("corrective_wave_a", failed["active_hypotheses"])

    def test_apparent_y_followed_by_x2_z_continuation(self) -> None:
        endpoint = create_initial_snapshot(_case())
        continued = advance_correction_snapshot(
            endpoint,
            cutoff=_date(12),
            structural_context={
                "x2_z_continuation": {
                    "observed_at": _date(12),
                    "positions": ["W", "X", "Y", "X2", "Z"],
                }
            },
        )
        self.assertEqual(_state(continued, "triple_three_continuation")["state"], "structurally_confirmed")
        self.assertIn("correction_complete", continued["invalidated_hypotheses"])
        self.assertIn("larger_wave_w_complete", continued["active_hypotheses"])
        self.assertIn("larger_wave_a_complete", continued["active_hypotheses"])


class PersistenceAndOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.directory.name) / "brain.sqlite3")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _persist(self, symbol: str = "NASDAQ:TEST") -> tuple[dict[str, object], dict[str, object]]:
        case = _case(symbol=symbol)
        snapshot = create_initial_snapshot(case)
        self.store.save_correction_case(case, initial_snapshot=snapshot)
        return case, snapshot

    def _resolve(self, hypothesis_id: str, symbol: str) -> tuple[dict[str, object], dict[str, object]]:
        case, snapshot = self._persist(symbol)
        saved = self.store.save_reviewed_outcome(_resolved_outcome(snapshot, hypothesis_id))
        return saved, self.store.get_current_reviewed_outcome(case["case_id"])

    def test_completed_wxy_later_resolved_as_entire_correction(self) -> None:
        saved, current = self._resolve("correction_complete", "NASDAQ:COMPLETE")
        self.assertEqual(saved["resolved_hypothesis"], "correction_complete")
        self.assertEqual(current["resolved_hypothesis"], "correction_complete")

    def test_completed_wxy_later_resolved_as_larger_w(self) -> None:
        saved, current = self._resolve("larger_wave_w_complete", "NASDAQ:LARGERW")
        self.assertEqual(saved["resolved_hypothesis"], "larger_wave_w_complete")
        self.assertEqual(current["resolved_hypothesis"], "larger_wave_w_complete")

    def test_completed_wxy_later_resolved_as_larger_a(self) -> None:
        saved, current = self._resolve("larger_wave_a_complete", "NASDAQ:LARGERA")
        self.assertEqual(saved["resolved_hypothesis"], "larger_wave_a_complete")
        self.assertEqual(current["resolved_hypothesis"], "larger_wave_a_complete")

    def test_reviewed_outcome_keeps_rejected_alternatives_and_reasons(self) -> None:
        case, snapshot = self._persist()
        self.store.save_reviewed_outcome(_resolved_outcome(snapshot, "correction_complete"))
        current = self.store.get_current_reviewed_outcome(case["case_id"])
        rejected = current["rejected_alternatives"]
        self.assertEqual(rejected[0]["hypothesis_id"], "correction_still_in_progress")
        self.assertTrue(rejected[0]["reason"])

    def test_review_does_not_rewrite_decision_time_snapshot(self) -> None:
        case, snapshot = self._persist()
        original = deepcopy(self.store.get_hypothesis_snapshot(snapshot["snapshot_id"]))
        self.store.save_reviewed_outcome(_resolved_outcome(snapshot, "correction_complete"))
        loaded = self.store.get_hypothesis_snapshot(snapshot["snapshot_id"])
        self.assertEqual(loaded, original)
        self.assertIsNone(loaded["timeline"]["final_reviewed_resolution_time"])
        self.assertFalse(loaded["reviewed"])
        self.assertEqual(self.store.get_correction_lineage(case["case_id"])["snapshots"][0], original)

    def test_multiple_snapshots_are_linked_in_order(self) -> None:
        case, endpoint = self._persist()
        child = advance_correction_snapshot(endpoint, cutoff=_date(3))
        grandchild = advance_correction_snapshot(child, cutoff=_date(5))
        self.store.save_hypothesis_snapshot(child)
        self.store.save_hypothesis_snapshot(grandchild)
        lineage = self.store.get_snapshot_lineage(grandchild["snapshot_id"])
        self.assertEqual([item["snapshot_id"] for item in lineage], [endpoint["snapshot_id"], child["snapshot_id"], grandchild["snapshot_id"]])
        self.assertEqual(len(self.store.get_correction_lineage(case["case_id"])["snapshots"]), 3)

    def test_revised_resolution_preserves_prior_review_history(self) -> None:
        case, snapshot = self._persist()
        first = self.store.save_reviewed_outcome(_resolved_outcome(snapshot, "correction_complete"))
        second = self.store.revise_outcome_resolution(_resolved_outcome(snapshot, "larger_wave_w_complete"))
        history = self.store.get_outcome_history(case["case_id"])
        self.assertEqual([item["outcome"]["revision"] for item in history], [1, 2])
        self.assertNotEqual(first["outcome_id"], second["outcome_id"])
        self.assertEqual(history[0]["reviews"][0]["action"], "approved")
        self.assertEqual(history[1]["reviews"][0]["action"], "revised")
        revised_outcome = history[1]["outcome"]
        self.assertTrue(
            any(
                item["hypothesis_id"] == "correction_complete"
                for item in revised_outcome["rejected_alternatives"]
            )
        )
        latest = self.store.get_latest_hypothesis_snapshot(case["case_id"])
        self.assertEqual(_state(latest, "correction_complete")["state"], "superseded")

    def test_rejected_proposal_does_not_resolve_case(self) -> None:
        case, snapshot = self._persist()
        proposed = self.store.propose_outcome_resolution(
            _resolved_outcome(snapshot, "correction_complete")
        )
        self.store.reject_outcome_resolution(
            proposed["outcome_id"], reviewer="second-reviewer", reason="Future structure contradicts it."
        )
        self.assertIsNone(self.store.get_current_reviewed_outcome(case["case_id"]))
        self.assertEqual(self.store.list_correction_cases(status="unresolved")[0]["case_id"], case["case_id"])

    def test_case_and_snapshot_deduplicate_by_stable_identity(self) -> None:
        case = _case()
        snapshot = create_initial_snapshot(case)
        first = self.store.save_correction_case(case, initial_snapshot=snapshot)
        second = self.store.save_correction_case(case, initial_snapshot=snapshot)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertFalse(second["initial_snapshot"]["inserted"])

    def test_export_contains_only_explicitly_reviewed_cases(self) -> None:
        reviewed_case, reviewed_snapshot = self._persist("NASDAQ:REVIEWED")
        self.store.save_reviewed_outcome(
            _resolved_outcome(reviewed_snapshot, "correction_complete")
        )
        self._persist("NASDAQ:OPEN")
        exported = self.store.export_reviewed_correction_cases()
        self.assertEqual(exported["case_count"], 1)
        self.assertEqual(exported["cases"][0]["case"]["case_id"], reviewed_case["case_id"])

    def test_clean_report_separates_endpoint_and_review_times(self) -> None:
        case, snapshot = self._persist()
        self.store.save_reviewed_outcome(_resolved_outcome(snapshot, "correction_complete"))
        report = render_correction_lineage_report(
            self.store.get_correction_lineage(case["case_id"])
        )
        self.assertIn("Candidate endpoint", report)
        self.assertIn("Final human review", report)
        self.assertIn("was not available at the endpoint snapshot", report)

    def test_invalidated_hypothesis_cannot_be_selected_for_review(self) -> None:
        case, snapshot = self._persist()
        invalidated = advance_correction_snapshot(
            snapshot,
            cutoff=_date(2),
            structural_invalidations=[
                {
                    "hypothesis_id": "correction_complete",
                    "basis": "hard_structural_rule",
                    "reason": "Impossible later sequence.",
                }
            ],
        )
        self.store.save_hypothesis_snapshot(invalidated)
        with self.assertRaises(ValueError):
            build_reviewed_outcome(
                invalidated,
                resolved_hypothesis="correction_complete",
                final_reviewed_interpretation="Invalid selection",
                resolution_cutoff=_date(20),
                reviewer="reviewer",
            )


class MigrationAndInterfaceTests(unittest.TestCase):
    def test_legacy_database_is_backed_up_and_not_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE legacy_marker(id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO legacy_marker(value) VALUES ('keep-me')")
            connection.commit()
            connection.close()
            store = KnowledgeStore(database)
            self.assertIsNotNone(store.phase4_backup_path)
            self.assertTrue(store.phase4_backup_path.exists())
            with store.connect() as migrated:
                self.assertEqual(migrated.execute("SELECT value FROM legacy_marker").fetchone()["value"], "keep-me")
                tables = {row["name"] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(
                {
                    "correction_cases",
                    "hypothesis_snapshots",
                    "hypothesis_states",
                    "hypothesis_transitions",
                    "resolved_outcomes",
                    "outcome_reviews",
                }.issubset(tables)
            )
            self.assertEqual(store.stats()["correction_cases"], 0)

    def test_versions_are_registered_without_probability_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "brain.sqlite3")
            with store.connect() as connection:
                rows = connection.execute(
                    "SELECT schema_version, calculation_version FROM correction_schema_versions"
                ).fetchall()
            versions = {row["schema_version"]: row["calculation_version"] for row in rows}
            self.assertEqual(versions[CORRECTION_STATE_SCHEMA_VERSION], CORRECTION_STATE_CALCULATION_VERSION)
            self.assertIn(OUTCOME_SCHEMA_VERSION, versions)
            self.assertNotIn("probability", json.dumps(versions).lower())

    def test_cli_exposes_review_and_audit_operations(self) -> None:
        parser = build_parser()
        for command in (
            "correction-cases",
            "correction-lineage",
            "correction-evidence",
            "correction-report",
            "resolve-correction",
            "reject-correction",
            "revise-correction",
            "export-corrections",
        ):
            with self.subTest(command=command):
                args = [command]
                if command in {"correction-lineage", "correction-report"}:
                    args.append("case-id")
                elif command == "correction-evidence":
                    args.append("snapshot-id")
                elif command in {"resolve-correction", "revise-correction"}:
                    args.extend(
                        [
                            "case-id",
                            "--snapshot",
                            "snapshot-id",
                            "--file",
                            "outcome.json",
                            "--reviewer",
                            "human",
                            "--resolution-cutoff",
                            _date(30),
                        ]
                    )
                elif command == "reject-correction":
                    args.extend(
                        ["outcome-id", "--reviewer", "human", "--reason", "wrong"]
                    )
                elif command == "export-corrections":
                    args.extend(["--output", "reviewed.json"])
                self.assertEqual(parser.parse_args(args).command, command)


if __name__ == "__main__":
    unittest.main()
