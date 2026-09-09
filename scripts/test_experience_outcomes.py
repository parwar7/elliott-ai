from __future__ import annotations

import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from elliott_ai.cli import build_parser, main
from elliott_ai.correction_state import (
    correction_case_content_hash,
    outcome_content_hash,
    review_content_hash,
)
from elliott_ai.experience import (
    experience_case_content_hash,
    pattern_dna_content_hash,
)
from elliott_ai.experience_analogue import compare_filtered_experiences
from elliott_ai.experience_comparison import filter_structurally_comparable_cases
from elliott_ai.experience_outcomes import (
    DEFAULT_HORIZON_ID,
    DEFAULT_HORIZON_VERSION,
    OBSERVATION_HORIZON_SPECIFICATION_VERSION,
    OUTCOME_EVIDENCE_CALCULATION_VERSION,
    OUTCOME_EVIDENCE_SCHEMA_VERSION,
    OUTCOME_EVIDENCE_SPECIFICATION_VERSION,
    OUTCOME_TAXONOMY_VERSION,
    attach_reviewed_outcome_evidence,
    observation_horizon_specification,
    outcome_evidence_content_hash,
    outcome_specification_manifest,
    outcome_taxonomy,
    render_experience_evidence,
    render_outcome_source_audit,
    render_outcome_specification,
)
from elliott_ai.experience_retrieval import (
    analogue_retrieval_content_hash,
    retrieve_analogues,
)
from elliott_ai.knowledge import KnowledgeStore
from scripts.test_experience_analogue import _rich
from scripts.test_experience_comparison import _current, _historical
from scripts.test_experience_engine import _persist_phase4_fixture
from scripts.test_experience_retrieval import _result


def _rehash_experience_case(source: dict[str, object]) -> None:
    record = source["experience_case_record"]
    case = record["experience_case"]
    case["content_hash"] = experience_case_content_hash(case)
    record["content_hash"] = case["content_hash"]


def _rehash_correction_case(source: dict[str, object]) -> None:
    case = source["correction_case"]
    case["content_hash"] = correction_case_content_hash(case)


def _rehash_outcome_links(source: dict[str, object]) -> None:
    outcome = source["outcome"]
    outcome["content_hash"] = outcome_content_hash(outcome)
    experience_case = source["experience_case_record"]["experience_case"]
    experience_case["source_references"]["outcome_content_hash"] = outcome[
        "content_hash"
    ]
    outcome_dna_row = source["resolved_outcome_dna_row"]
    outcome_dna = outcome_dna_row["dna"]
    outcome_dna["source_outcome_hash"] = outcome["content_hash"]
    outcome_dna["content_hash"] = pattern_dna_content_hash(outcome_dna)
    outcome_dna_row["content_hash"] = outcome_dna["content_hash"]
    _rehash_experience_case(source)


def _rehash_outcome_review_links(source: dict[str, object]) -> None:
    review = source["outcome_review"]
    review["content_hash"] = review_content_hash(review)
    experience_case = source["experience_case_record"]["experience_case"]
    experience_case["source_references"]["outcome_review_hash"] = review[
        "content_hash"
    ]
    outcome_dna_row = source["resolved_outcome_dna_row"]
    outcome_dna = outcome_dna_row["dna"]
    outcome_dna["source_outcome_review_hash"] = review["content_hash"]
    outcome_dna["content_hash"] = pattern_dna_content_hash(outcome_dna)
    outcome_dna_row["content_hash"] = outcome_dna["content_hash"]
    _rehash_experience_case(source)


def _retarget_retrieval(
    sources: list[tuple[str, dict[str, object] | None]],
) -> dict[str, object]:
    result = deepcopy(_result())
    template = result["ordered_analogue_references"][0]
    analogues: list[dict[str, object]] = []
    for position, (case_id, source) in enumerate(sources, start=1):
        analogue = deepcopy(template)
        if source is None:
            market_episode_id = f"missing-episode-{position}"
            case_version = 1
            endpoint_hash = f"e{position}" * 32
            fingerprint_hash = f"f{position}" * 32
            snapshot_hash = f"s{position}" * 32
        else:
            storage = source["experience_case_record"]
            case = storage["experience_case"]
            market_episode_id = str(case["market_episode_id"])
            case_version = int(storage["case_version"])
            endpoint_hash = str(source["endpoint_dna_row"]["dna"]["content_hash"])
            fingerprint_hash = str(
                source["fingerprint_record"]["fingerprint"]["content_hash"]
            )
            snapshot_hash = str(source["endpoint_snapshot"]["content_hash"])
        analogue["experience_case_reference"] = {
            "experience_case_id": case_id,
            "market_episode_id": market_episode_id,
            "case_version": case_version,
        }
        analogue["presentation_order"] = position
        analogue["immutable_input_references"][
            "historical_endpoint_dna_hash"
        ] = endpoint_hash
        analogue["immutable_input_references"][
            "historical_phase3_fingerprint_hash"
        ] = fingerprint_hash
        analogue["immutable_input_references"][
            "historical_phase4_endpoint_snapshot_hash"
        ] = snapshot_hash
        analogues.append(analogue)
    result["ordered_analogue_references"] = analogues
    result["presented_analogue_count"] = len(analogues)
    result["eligible_candidate_count"] = len(analogues)
    result["not_presented_due_to_limit"] = 0
    result["status"] = (
        "available" if analogues else "unavailable_empty_accepted_experience_pool"
    )
    result["content_hash"] = analogue_retrieval_content_hash(result)
    return result


def _evidence(result: dict[str, object], index: int = 0) -> dict[str, object]:
    return result["analogue_evidence"][index]["historical_reviewed_outcome"]


class OutcomeEvidenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "experience.sqlite3"
        self.store = KnowledgeStore(self.database)
        self.phase4 = _persist_phase4_fixture(self.store)
        created = self.store.create_experience_case(
            str(self.phase4["case"]["case_id"])
        )
        self.case_id = str(created["experience_case_id"])
        self.store.review_experience_case(
            self.case_id,
            action="accept",
            reviewer="Parwa",
            rationale="Accepted deterministic Phase 5B test fixture.",
            human_confirmed=True,
            reviewed_at="2026-03-03T00:00:00+00:00",
        )
        self.source = self.store.get_experience_outcome_source(self.case_id)
        self.retrieval = _retarget_retrieval([(self.case_id, self.source)])

    def attach(
        self,
        source: dict[str, object] | None = None,
        retrieval: dict[str, object] | None = None,
    ) -> dict[str, object]:
        selected_source = self.source if source is None else source
        return attach_reviewed_outcome_evidence(
            retrieval or self.retrieval,
            lambda _case_id: selected_source,
        )


class SpecificationAndAcceptedEvidenceTests(OutcomeEvidenceFixture):
    def test_specification_taxonomy_horizon_and_hashes_are_versioned(self) -> None:
        manifest = outcome_specification_manifest()
        self.assertEqual(
            manifest["outcome_evidence_specification"]["specification_version"],
            OUTCOME_EVIDENCE_SPECIFICATION_VERSION,
        )
        self.assertEqual(
            manifest["outcome_taxonomy"]["taxonomy_version"],
            OUTCOME_TAXONOMY_VERSION,
        )
        self.assertEqual(
            manifest["observation_horizon_specification"][
                "specification_version"
            ],
            OBSERVATION_HORIZON_SPECIFICATION_VERSION,
        )
        self.assertEqual(manifest, outcome_specification_manifest())
        self.assertEqual(outcome_taxonomy(), outcome_taxonomy())

    def test_invalid_horizon_id_and_version_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported observation horizon"):
            observation_horizon_specification("unknown")
        with self.assertRaisesRegex(ValueError, "horizon version"):
            observation_horizon_specification(
                DEFAULT_HORIZON_ID, "99.0.0"
            )

    def test_accepted_outcome_links_identity_review_and_structure(self) -> None:
        result = self.attach()
        evidence = _evidence(result)
        self.assertEqual(result["schema_version"], OUTCOME_EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(
            result["calculation_version"], OUTCOME_EVIDENCE_CALCULATION_VERSION
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(evidence["evidence_state"], "accepted")
        self.assertEqual(
            evidence["structural_outcome"]["taxonomy_category"],
            "reviewed_endpoint_completion",
        )
        self.assertEqual(
            evidence["identity_and_linkage"]["experience_case_id"], self.case_id
        )
        self.assertEqual(evidence["review_state"]["reviewer"], "Parwa")
        self.assertEqual(
            evidence["content_hash"], outcome_evidence_content_hash(evidence)
        )

    def test_deterministic_measurements_and_unavailable_price_metrics(self) -> None:
        evidence = _evidence(self.attach())
        window = evidence["observation_window"]
        self.assertEqual(window["horizon_id"], DEFAULT_HORIZON_ID)
        self.assertEqual(window["elapsed"]["clock_days"], 21.0)
        self.assertEqual(window["completeness_state"], "complete")
        self.assertEqual(window["censoring_state"], "not_censored")
        first_event = evidence["timing_behavior"][
            "first_recorded_structural_event"
        ]
        self.assertEqual(first_event["status"], "available")
        self.assertEqual(
            evidence["price_behavior"]["maximum_favorable_excursion"]["status"],
            "unavailable",
        )
        self.assertIn(
            "No immutable post-endpoint candle series",
            evidence["price_behavior"]["maximum_adverse_excursion"]["reason"],
        )

    def test_repeated_runs_are_deterministic_and_do_not_mutate_inputs(self) -> None:
        retrieval_before = deepcopy(self.retrieval)
        source_before = deepcopy(self.source)
        first = self.attach()
        second = self.attach()
        self.assertEqual(first, second)
        self.assertEqual(self.retrieval, retrieval_before)
        self.assertEqual(self.source, source_before)
        self.assertEqual(first["content_hash"], second["content_hash"])

    def test_retrieval_and_outcome_evidence_remain_separate(self) -> None:
        result = self.attach()
        item = result["analogue_evidence"][0]
        self.assertIn("retrieval_evidence", item)
        self.assertIn("historical_reviewed_outcome", item)
        self.assertNotIn(
            "resolved_hypothesis", json.dumps(item["retrieval_evidence"])
        )
        self.assertEqual(
            result["frozen_phase5a4_retrieval"]["retrieval_content_hash"],
            self.retrieval["content_hash"],
        )


class AvailabilityAndIntegrityStateTests(OutcomeEvidenceFixture):
    def test_no_retrieved_analogues_and_empty_pool_do_not_call_loader(self) -> None:
        empty = _retarget_retrieval([])
        loader = Mock()
        result = attach_reviewed_outcome_evidence(empty, loader)
        self.assertEqual(result["status"], "unavailable_no_retrieved_analogues")
        self.assertEqual(result["analogue_evidence"], [])
        loader.assert_not_called()

    def test_missing_unreviewed_partial_rejected_and_stale_states(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []

        no_outcome = deepcopy(self.source)
        no_outcome["outcome"] = None
        cases.append(("unavailable_no_outcome", no_outcome))

        unreviewed = deepcopy(self.source)
        unreviewed["outcome_review"] = None
        cases.append(("unavailable_unreviewed_outcome", unreviewed))

        partial = deepcopy(self.source)
        partial["outcome_review"]["review_status"] = "partially_reviewed"
        _rehash_outcome_review_links(partial)
        cases.append(("unavailable_partially_reviewed_outcome", partial))

        rejected = deepcopy(self.source)
        rejected["outcome_review"]["action"] = "rejected"
        rejected["outcome_review"]["review_status"] = "rejected"
        _rehash_outcome_review_links(rejected)
        cases.append(("rejected_outcome", rejected))

        stale = deepcopy(self.source)
        stale["outcome_is_current"] = False
        cases.append(("unavailable_stale_outcome", stale))

        for expected, source in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_evidence(self.attach(source))["evidence_state"], expected)

    def test_nonaccepted_experience_and_source_load_error_are_explicit(self) -> None:
        not_accepted = deepcopy(self.source)
        not_accepted["effective_status"]["state"] = "rejected"
        not_accepted["effective_status"]["accepted_pool_eligible"] = False
        self.assertEqual(
            _evidence(self.attach(not_accepted))["evidence_state"],
            "unavailable_experience_not_accepted",
        )
        load_error = {
            "load_status": "read_failure",
            "reason": "fixture read failed",
        }
        self.assertEqual(
            _evidence(self.attach(load_error))["evidence_state"],
            "source_load_error",
        )

    def test_outcome_hash_failure_is_rejected(self) -> None:
        broken = deepcopy(self.source)
        broken["outcome"]["final_reviewed_interpretation"] = "tampered"
        evidence = _evidence(self.attach(broken))
        self.assertEqual(evidence["evidence_state"], "invalid_hash")
        self.assertIn("outcome_hash_failure", {item["code"] for item in evidence["issues"]})

    def test_linkage_and_identity_mismatches_are_rejected(self) -> None:
        mutations = []
        for code, field, value in (
            ("symbol_mismatch", "symbol", "NYSE:OTHER"),
            ("timeframe_mismatch", "timeframe", "weekly"),
            ("degree_mismatch", "elliott_degree", "Primary"),
        ):
            source = deepcopy(self.source)
            source["correction_case"][field] = value
            _rehash_correction_case(source)
            mutations.append((code, source, deepcopy(self.retrieval)))

        role = deepcopy(self.source)
        role["correction_case"]["candidate_endpoint"]["terminal_label"] = "C"
        _rehash_correction_case(role)
        mutations.append(("role_mismatch", role, deepcopy(self.retrieval)))

        for code, field, value in (
            ("feed_mismatch", "feed_identity", "other|NASDAQ:TEST|daily"),
            ("provenance_mismatch", "provider", "other-provider"),
        ):
            source = deepcopy(self.source)
            source["correction_case"]["source_identity"][field] = value
            _rehash_correction_case(source)
            mutations.append((code, source, deepcopy(self.retrieval)))

        fingerprint_retrieval = deepcopy(self.retrieval)
        fingerprint_retrieval["ordered_analogue_references"][0][
            "immutable_input_references"
        ]["historical_phase3_fingerprint_hash"] = "0" * 64
        fingerprint_retrieval["content_hash"] = analogue_retrieval_content_hash(
            fingerprint_retrieval
        )
        mutations.append(
            (
                "retrieval_historical_phase3_fingerprint_hash_mismatch",
                deepcopy(self.source),
                fingerprint_retrieval,
            )
        )

        snapshot_retrieval = deepcopy(self.retrieval)
        snapshot_retrieval["ordered_analogue_references"][0][
            "immutable_input_references"
        ]["historical_phase4_endpoint_snapshot_hash"] = "0" * 64
        snapshot_retrieval["content_hash"] = analogue_retrieval_content_hash(
            snapshot_retrieval
        )
        mutations.append(
            (
                "retrieval_historical_phase4_endpoint_snapshot_hash_mismatch",
                deepcopy(self.source),
                snapshot_retrieval,
            )
        )

        for code, source, retrieval in mutations:
            with self.subTest(code=code):
                evidence = _evidence(self.attach(source, retrieval))
                self.assertEqual(evidence["evidence_state"], "invalid_linkage")
                self.assertIn(code, {item["code"] for item in evidence["issues"]})

    def test_storage_linkage_and_pre_endpoint_outcome_are_rejected(self) -> None:
        broken_link = deepcopy(self.source)
        broken_link["experience_case_record"]["source_outcome_id"] = "other-outcome"
        evidence = _evidence(self.attach(broken_link))
        self.assertEqual(evidence["evidence_state"], "invalid_linkage")
        self.assertIn(
            "outcome_id_storage_link_mismatch",
            {item["code"] for item in evidence["issues"]},
        )

        before_endpoint = deepcopy(self.source)
        before_endpoint["outcome"]["resolution_cutoff"] = "2026-02-01T00:00:00+00:00"
        _rehash_outcome_links(before_endpoint)
        evidence = _evidence(self.attach(before_endpoint))
        self.assertEqual(evidence["evidence_state"], "invalid_linkage")
        self.assertIn(
            "outcome_not_after_endpoint",
            {item["code"] for item in evidence["issues"]},
        )

    def test_incomplete_horizon_is_right_censored(self) -> None:
        source = deepcopy(self.source)
        row = source["confirmation_dna_row"]
        row["dna"]["cutoff"] = "2026-02-20T00:00:00+00:00"
        row["dna"]["content_hash"] = pattern_dna_content_hash(row["dna"])
        row["content_hash"] = row["dna"]["content_hash"]
        evidence = _evidence(self.attach(source))
        window = evidence["observation_window"]
        self.assertEqual(window["completeness_state"], "incomplete")
        self.assertEqual(window["censoring_state"], "right_censored")

    def test_missing_market_series_is_never_silently_omitted(self) -> None:
        evidence = _evidence(self.attach())
        self.assertEqual(
            evidence["quality_and_limitations"]["missing_intervals"],
            "unavailable_without_post_endpoint_candle_series",
        )
        self.assertEqual(
            evidence["momentum_and_volume_aftermath"]["trendline_retest_behavior"][
                "status"
            ],
            "unavailable",
        )

    def test_reviewer_disagreement_is_an_accepted_limitation(self) -> None:
        source = deepcopy(self.source)
        source["outcome_review"]["reviewer"] = "Second Reviewer"
        _rehash_outcome_review_links(source)
        evidence = _evidence(self.attach(source))
        self.assertEqual(evidence["evidence_state"], "accepted_with_limitations")
        self.assertTrue(evidence["review_state"]["reviewer_disagreement"])
        self.assertIn(
            "reviewer_disagreement", {item["code"] for item in evidence["issues"]}
        )

    def test_mixed_available_and_unavailable_outcomes_preserve_order(self) -> None:
        missing_id = "missing-outcome-case"
        retrieval = _retarget_retrieval(
            [(self.case_id, self.source), (missing_id, None)]
        )
        source_map = {self.case_id: self.source, missing_id: None}
        result = attach_reviewed_outcome_evidence(
            retrieval, lambda case_id: source_map[case_id]
        )
        self.assertEqual(result["status"], "completed_mixed_outcome_availability")
        self.assertEqual(result["counts"]["accepted_outcomes"], 1)
        self.assertEqual(result["counts"]["unavailable_or_invalid_outcomes"], 1)
        self.assertEqual(
            [
                item["historical_reviewed_outcome"]["identity_and_linkage"][
                    "retrieval_position"
                ]
                for item in result["analogue_evidence"]
            ],
            [1, 2],
        )


class LeakageIsolationTests(OutcomeEvidenceFixture):
    def test_changed_outcome_cannot_change_phase5a2_a3_or_a4(self) -> None:
        current = _rich(_current())
        historical = _rich(_historical())
        baseline_filter = filter_structurally_comparable_cases(current, [historical])
        baseline_comparison = compare_filtered_experiences(
            current, [historical], baseline_filter
        )
        baseline_retrieval = retrieve_analogues(baseline_comparison)

        changed_current = deepcopy(current)
        changed_historical = deepcopy(historical)
        changed_current["reviewed_outcome"] = {"label": "changed-current"}
        changed_historical["reviewed_outcome"] = {
            "label": "changed-historical",
            "movement": 1_000_000,
        }
        changed_filter = filter_structurally_comparable_cases(
            changed_current, [changed_historical]
        )
        changed_comparison = compare_filtered_experiences(
            changed_current, [changed_historical], changed_filter
        )
        changed_retrieval = retrieve_analogues(changed_comparison)

        self.assertEqual(baseline_filter, changed_filter)
        self.assertEqual(baseline_comparison, changed_comparison)
        self.assertEqual(
            [item["retrieval_tier"] for item in baseline_retrieval["ordered_analogue_references"]],
            [item["retrieval_tier"] for item in changed_retrieval["ordered_analogue_references"]],
        )
        self.assertEqual(
            [item["experience_case_reference"] for item in baseline_retrieval["ordered_analogue_references"]],
            [item["experience_case_reference"] for item in changed_retrieval["ordered_analogue_references"]],
        )
        self.assertEqual(baseline_retrieval, changed_retrieval)
        self.assertEqual(
            baseline_retrieval["content_hash"], changed_retrieval["content_hash"]
        )

    def test_favorable_or_unfavorable_wording_cannot_promote_or_demote(self) -> None:
        favorable = deepcopy(self.source)
        favorable["outcome"]["final_reviewed_interpretation"] = (
            "Historically favorable fixture wording."
        )
        _rehash_outcome_links(favorable)
        unfavorable = deepcopy(self.source)
        unfavorable["outcome"]["final_reviewed_interpretation"] = (
            "Historically unfavorable fixture wording."
        )
        _rehash_outcome_links(unfavorable)

        first = self.attach(favorable)
        second = self.attach(unfavorable)
        for result in (first, second):
            isolation = result["retrieval_outcome_isolation"]
            self.assertEqual(
                isolation["retrieval_hash_before_outcomes"],
                self.retrieval["content_hash"],
            )
            self.assertTrue(isolation["retrieval_hash_unchanged"])
            self.assertTrue(isolation["retrieval_order_preserved"])
            self.assertTrue(isolation["retrieval_tiers_preserved"])
        self.assertEqual(
            first["frozen_phase5a4_retrieval"],
            second["frozen_phase5a4_retrieval"],
        )

    def test_nonretrieved_outcome_is_never_loaded_or_selected(self) -> None:
        extra_id = "not-retrieved-case"
        loaded: list[str] = []
        source_map = {self.case_id: self.source, extra_id: deepcopy(self.source)}

        def loader(case_id: str) -> dict[str, object] | None:
            loaded.append(case_id)
            return source_map.get(case_id)

        result = attach_reviewed_outcome_evidence(self.retrieval, loader)
        self.assertEqual(loaded, [self.case_id])
        self.assertNotIn(extra_id, json.dumps(result))

    def test_outcome_loader_runs_only_after_valid_retrieval_is_frozen(self) -> None:
        invalid = deepcopy(self.retrieval)
        invalid["content_hash"] = "0" * 64
        loader = Mock(return_value=self.source)
        with self.assertRaisesRegex(ValueError, "retrieval content hash"):
            attach_reviewed_outcome_evidence(invalid, loader)
        loader.assert_not_called()

        result = self.attach()
        trace = result["retrieval_outcome_isolation"]["execution_trace"]
        self.assertEqual(trace[0], "phase5a4_retrieval_validated_and_frozen")
        self.assertTrue(trace[1].startswith("outcome_source_loaded:"))

    def test_retrieval_limit_tier_order_and_hash_are_immutable(self) -> None:
        before = deepcopy(self.retrieval)
        result = self.attach()
        isolation = result["retrieval_outcome_isolation"]
        self.assertEqual(
            result["original_phase5a4_retrieval"]["requested_result_limit"],
            before["requested_result_limit"],
        )
        self.assertEqual(
            result["original_phase5a4_retrieval"]["ordered_analogue_references"],
            before["ordered_analogue_references"],
        )
        self.assertEqual(
            isolation["retrieval_hash_after_outcomes"], before["content_hash"]
        )

    def test_phase3_phase4_and_database_records_are_not_mutated(self) -> None:
        fingerprint_before = deepcopy(
            self.store.get_wave_fingerprint(
                int(self.source["experience_case_record"]["source_fingerprint_id"])
            )
        )
        snapshot_id = str(
            self.source["experience_case_record"]["source_endpoint_snapshot_id"]
        )
        snapshot_before = deepcopy(self.store.get_hypothesis_snapshot(snapshot_id))
        self.store.build_experience_evidence(self.case_id)
        self.assertEqual(
            self.store.get_wave_fingerprint(
                int(self.source["experience_case_record"]["source_fingerprint_id"])
            ),
            fingerprint_before,
        )
        self.assertEqual(self.store.get_hypothesis_snapshot(snapshot_id), snapshot_before)


class RenderingCliAndDatabaseTests(OutcomeEvidenceFixture):
    def test_renderers_are_deterministic_and_nonpredictive(self) -> None:
        manifest_text = render_outcome_specification(outcome_specification_manifest())
        audit = self.store.inspect_experience_outcome(self.case_id)
        audit_text = render_outcome_source_audit(
            audit,
            explain=True,
            show_provenance=True,
            show_hashes=True,
            include_outcome_details=True,
        )
        result_text = render_experience_evidence(
            self.attach(),
            explain=True,
            show_provenance=True,
            show_hashes=True,
        )
        self.assertIn("# Historical Outcome Evidence Specification", manifest_text)
        self.assertIn("# Historical Reviewed Outcome Audit", audit_text)
        self.assertIn("source audit only", audit_text)
        self.assertIn("# Retrieved Analogues With Historical Reviewed Outcomes", result_text)
        self.assertIn("Retrieval Evidence", result_text)
        self.assertIn("Historical Reviewed Outcome", result_text)
        self.assertIn("contextual evidence, not a forecast", result_text)
        for forbidden in ("expected return", "winning analogue", "success rate"):
            self.assertNotIn(forbidden, result_text.lower())

    def test_cli_parser_exposes_phase5b_controls(self) -> None:
        args = build_parser().parse_args(
            [
                "experience",
                "evidence",
                "current-case",
                "--candidate",
                "historical-case",
                "--comparison-level",
                "3",
                "--limit",
                "4",
                "--horizon",
                DEFAULT_HORIZON_ID,
                "--horizon-version",
                DEFAULT_HORIZON_VERSION,
                "--format",
                "text",
                "--explain",
                "--show-provenance",
                "--show-hashes",
                "--include-retrieval-details",
                "--include-outcome-details",
            ]
        )
        self.assertEqual(args.experience_command, "evidence")
        self.assertEqual(args.comparison_level, 3)
        self.assertEqual(args.limit, 4)
        self.assertTrue(args.include_retrieval_details)
        self.assertTrue(args.include_outcome_details)

    def test_cli_outcome_spec_json_and_text(self) -> None:
        manifest = outcome_specification_manifest()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.experience_outcome_specification.return_value = manifest
            json_output = io.StringIO()
            with patch("sys.stdout", json_output):
                main(["experience", "outcome-spec", "--format", "json"])
            text_output = io.StringIO()
            with patch("sys.stdout", text_output):
                main(["experience", "outcome-spec", "--format", "text"])
        self.assertIn(OUTCOME_TAXONOMY_VERSION, json_output.getvalue())
        self.assertIn("# Historical Outcome Evidence Specification", text_output.getvalue())

    def test_cli_outcomes_json_and_human_readable_output(self) -> None:
        audit = self.store.inspect_experience_outcome(self.case_id)
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.inspect_experience_outcome.return_value = audit
            json_output = io.StringIO()
            with patch("sys.stdout", json_output):
                main(["experience", "outcomes", self.case_id, "--format", "json"])
            text_output = io.StringIO()
            with patch("sys.stdout", text_output):
                main(
                    [
                        "experience",
                        "outcomes",
                        self.case_id,
                        "--format",
                        "text",
                        "--explain",
                        "--show-provenance",
                        "--show-hashes",
                    ]
                )
        self.assertIn('"historical_reviewed_outcome"', json_output.getvalue())
        self.assertIn("# Historical Reviewed Outcome Audit", text_output.getvalue())
        self.assertIn("## Provenance", text_output.getvalue())
        self.assertIn("## Hashes", text_output.getvalue())

    def test_cli_evidence_json_and_human_readable_output(self) -> None:
        result = self.attach()
        with patch("elliott_ai.cli.KnowledgeStore") as store_class:
            store_class.return_value.build_experience_evidence.return_value = result
            json_output = io.StringIO()
            with patch("sys.stdout", json_output):
                main(["experience", "evidence", "current", "--format", "json"])
            text_output = io.StringIO()
            with patch("sys.stdout", text_output):
                main(
                    [
                        "experience",
                        "evidence",
                        "current",
                        "--format",
                        "text",
                        "--explain",
                        "--show-provenance",
                        "--show-hashes",
                    ]
                )
        self.assertIn(OUTCOME_EVIDENCE_SCHEMA_VERSION, json_output.getvalue())
        self.assertIn("# Retrieved Analogues With Historical Reviewed Outcomes", text_output.getvalue())
        self.assertIn("Frozen retrieval hash:", text_output.getvalue())

    def test_store_has_no_migration_and_sqlite_integrity_holds(self) -> None:
        with self.store.connect() as connection:
            tables_before = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        _ = self.store.inspect_experience_outcome(self.case_id)
        _ = self.store.build_experience_evidence(self.case_id)
        with self.store.connect() as connection:
            tables_after = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(tables_before, tables_after)
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])

    def test_store_empty_accepted_pool_is_valid(self) -> None:
        result = self.store.build_experience_evidence(self.case_id)
        self.assertEqual(result["status"], "unavailable_no_retrieved_analogues")
        self.assertEqual(result["counts"]["retrieved_analogues"], 0)
        self.assertTrue(
            result["retrieval_outcome_isolation"]["only_selected_cases_loaded"]
        )


if __name__ == "__main__":
    unittest.main()
