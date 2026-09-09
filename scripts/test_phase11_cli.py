from __future__ import annotations

import ast
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path

from elliott_ai.cli import build_parser, main
from elliott_ai.config import Settings
from elliott_ai.forecast_outcomes import (
    EvaluationPolicy,
    ForecastObservationSet,
    ObservationScheduleMode,
    build_forecast_observation_set,
)
from elliott_ai.forecast_records import (
    ExpectedCompletionWindow,
    ForecastDirection,
    InvalidationClaim,
    InvalidationOperator,
    TargetClaim,
)
from elliott_ai.knowledge import KnowledgeStore
from elliott_ai.mistake_memory import (
    FailureDiagnosis,
    FailureDiagnosisType,
    LessonScopeType,
    LessonStatus,
    ReviewDecision,
)
from elliott_ai.outcome_learning_agents import OutcomeLearningAgentRole
from elliott_ai.phase11_cli import (
    PHASE11_SHADOW_STATE_DIRECTORY,
    ManualShadowWorkflowCheckpoint,
    Phase11CheckpointStore,
    execute_phase11_shadow_command,
)
from elliott_ai.phase11_shadow_workflow import ShadowWorkflowResult
from elliott_ai.technical_agent_orchestrator import TechnicalAgentRole

try:
    from scripts.test_outcome_learning_agents import (
        FixtureProvider as D2FixtureProvider,
    )
    from scripts.test_phase11_shadow_workflow import (
        APPROVED_AT,
        DRAFTED_AT,
        EVALUATED_AT,
        HORIZON_END,
        OBSERVED_AT,
        PROPOSED_AT,
        REVIEWED_AT,
    )
    from scripts.test_technical_agent_orchestrator import (
        CUTOFF,
        EXECUTED,
        FixtureProvider as D1FixtureProvider,
        _source_records,
    )
except ModuleNotFoundError:  # Direct execution from scripts.
    from test_outcome_learning_agents import FixtureProvider as D2FixtureProvider
    from test_phase11_shadow_workflow import (
        APPROVED_AT,
        DRAFTED_AT,
        EVALUATED_AT,
        HORIZON_END,
        OBSERVED_AT,
        PROPOSED_AT,
        REVIEWED_AT,
    )
    from test_technical_agent_orchestrator import (
        CUTOFF,
        EXECUTED,
        FixtureProvider as D1FixtureProvider,
        _source_records,
    )


ACTIVATED_AT = "2026-01-10T21:07:00+00:00"
LESSON_RECORDED_AT = "2026-01-10T21:06:00+00:00"


class CompositeFixtureProvider:
    """One strict provider facade for the D1 and D2 fixture agents."""

    name = "openai"
    model = "fixture-v1"

    def __init__(self, *, d1: D1FixtureProvider | None = None) -> None:
        self.d1 = d1 or D1FixtureProvider()
        self.d2 = D2FixtureProvider()
        self.calls: list[str] = []

    def generate_strict_json(self, **kwargs):
        role = kwargs["packet"]["agent_role"]
        self.calls.append(role)
        if role in {item.value for item in OutcomeLearningAgentRole}:
            return self.d2.generate_strict_json(**kwargs)
        return self.d1.generate_strict_json(**kwargs)

    def generate(self, **kwargs):
        return self.generate_strict_json(**kwargs)


class Phase11ManualCliFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "brain.sqlite3"
        self.settings = Settings.from_environment(
            workspace=self.root,
            database_path=self.database,
            provider="packet",
            model=None,
        )
        self.store = KnowledgeStore(self.database)
        self.run, self.resolution = _source_records(self.store)
        self.provider = CompositeFixtureProvider()
        self.provider_settings: list[tuple[str, str | None]] = []
        self.parser = build_parser()

    @property
    def checkpoint_root(self) -> Path:
        return self.database.parent / PHASE11_SHADOW_STATE_DIRECTORY

    def provider_factory(self, settings: Settings):
        self.provider_settings.append((settings.provider, settings.model))
        return self.provider

    def execute(self, arguments: list[str]) -> dict:
        args = self.parser.parse_args(["phase11-shadow", *arguments])
        return execute_phase11_shadow_command(
            args,
            settings=self.settings,
            store=self.store,
            provider_factory=self.provider_factory,
        )

    def write_json(self, name: str, value) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(value, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def start_arguments(self, *, allow: bool = True, commit: bool = True) -> list[str]:
        values = [
            "start",
            "--analysis-run-id",
            str(self.run["id"]),
            "--degree-resolution-id",
            str(self.resolution["id"]),
            "--cutoff",
            CUTOFF,
            "--executed-at",
            EXECUTED,
            "--provider",
            "openai",
            "--model",
            "fixture-v1",
            "--shadow",
            "--blind",
        ]
        if allow:
            values.append("--allow-model-call")
        if commit:
            values.append("--commit")
        return values

    def start(self) -> dict:
        return self.execute(self.start_arguments())

    def claims_file(self, start: dict, *, candidate_id: str | None = None) -> Path:
        result = ShadowWorkflowResult.from_dict(start["result"])
        proposal = result.proposal
        assert proposal is not None
        selected = candidate_id or proposal.proposed_selected_candidate_id
        assert selected is not None
        claims = {
            "target_claims": [
                TargetClaim(
                    claim_id=f"target-{selected}",
                    hypothesis_id=selected,
                    target_low=140.0,
                    target_high=145.0,
                    direction=ForecastDirection.UP,
                    timeframe="daily",
                    price_basis="ohlc",
                    rationale="Frozen manual CLI fixture target.",
                ).to_dict()
            ],
            "invalidation_claims": [
                InvalidationClaim(
                    claim_id=f"invalidate-{selected}",
                    hypothesis_id=selected,
                    operator=InvalidationOperator.AT_OR_BELOW,
                    condition="Price reaches or breaches 90.",
                    timeframe="daily",
                    price_basis="ohlc",
                    price_level=90.0,
                ).to_dict()
            ],
            "confirmation_claims": [],
            "expected_completion_windows": [
                ExpectedCompletionWindow(
                    window_id=f"window-{selected}",
                    hypothesis_id=selected,
                    start_utc=CUTOFF,
                    end_utc=HORIZON_END,
                    timeframe="daily",
                    start_rule="First completed post-cutoff candle.",
                    end_rule="Frozen five-session horizon.",
                    rationale="Deterministic manual CLI fixture horizon.",
                ).to_dict()
            ],
        }
        return self.write_json("claims.json", claims)

    def approve(
        self,
        start: dict | None = None,
        *,
        candidate_id: str | None = None,
        commit: bool = True,
    ) -> dict:
        started = start or self.start()
        result = ShadowWorkflowResult.from_dict(started["result"])
        proposal = result.proposal
        assert proposal is not None
        selected = candidate_id or proposal.proposed_selected_candidate_id
        assert selected is not None
        values = [
            "approve-forecast",
            result.workflow_id,
            "--checkpoint-hash",
            started["checkpoint"]["content_hash"],
            "--proposal-hash",
            proposal.content_hash,
            "--confirm-proposal-hash",
            proposal.content_hash,
            "--selected-candidate-id",
            selected,
            "--human-actor",
            "human:operator-1",
            "--approved-at",
            APPROVED_AT,
            "--direction",
            ForecastDirection.UP.value,
            "--claims-file",
            str(self.claims_file(started, candidate_id=selected)),
            "--approval-note",
            "Human approved this exact candidate and the typed claims.",
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def observation_file(self, approved: dict, *, failure: bool = True) -> Path:
        result = ShadowWorkflowResult.from_dict(approved["result"])
        forecast = self.store.get_forecast_record(result.forecast_id)
        assert forecast is not None
        start = datetime.fromisoformat(forecast.analysis_cutoff_utc)
        expected_opens = tuple(
            (start + timedelta(days=index)).isoformat(timespec="seconds")
            for index in range(5)
        )
        rows = []
        for index in range(5):
            opened = start + timedelta(days=index)
            rows.append(
                {
                    "open_time_utc": opened.isoformat(timespec="seconds"),
                    "close_time_utc": (opened + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                    "open": 100.0,
                    "high": 110.0,
                    "low": 85.0 if failure and index == 1 else 95.0,
                    "close": 102.0,
                    "volume": 1_000.0 + index,
                    "source_sequence": index,
                }
            )
        observation = build_forecast_observation_set(
            forecast,
            rows,
            source_dataset_id=forecast.dataset_cutoffs[0].dataset_id,
            actual_evaluation_cutoff_utc=HORIZON_END,
            evaluation_policy=EvaluationPolicy.create(
                expected_interval_seconds=86_400,
                schedule_mode=ObservationScheduleMode.EXPLICIT_EXPECTED_OPENS,
            ),
            expected_open_times_utc=expected_opens,
        )
        return self.write_json("observations.json", observation.to_dict())

    def register(self, approved: dict | None = None, *, commit: bool = True) -> dict:
        selected = approved or self.approve()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        values = [
            "register-observations",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--observation-file",
            str(self.observation_file(selected)),
            "--accepted-at",
            OBSERVED_AT,
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def evaluate(self, registered: dict | None = None, *, commit: bool = True) -> dict:
        selected = registered or self.register()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        values = [
            "evaluate",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--evaluated-at",
            EVALUATED_AT,
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def draft_review(self, evaluated: dict | None = None, *, commit: bool = True) -> dict:
        selected = evaluated or self.evaluate()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        values = [
            "draft-review",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--forecast-id",
            result.forecast_id,
            "--forecast-hash",
            result.forecast_hash,
            "--observation-set-id",
            result.observation_set_id,
            "--observation-set-hash",
            result.observation_set_hash,
            "--evaluation-id",
            result.evaluation_id,
            "--evaluation-hash",
            result.evaluation_hash,
            "--drafted-at",
            DRAFTED_AT,
            "--provider",
            "openai",
            "--model",
            "fixture-v1",
            "--shadow",
            "--allow-model-call",
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def diagnoses_file(self, hypothesis_id: str) -> Path:
        diagnosis = FailureDiagnosis(
            diagnosis_id=f"diagnosis-{hypothesis_id}",
            diagnosis_type=FailureDiagnosisType.PREMATURE_COMPLETION,
            summary="The frozen main count failed its deterministic invalidation.",
            affected_hypothesis_ids=(hypothesis_id,),
        )
        return self.write_json("diagnoses.json", [diagnosis.to_dict()])

    def record_review(self, drafted: dict | None = None, *, commit: bool = True) -> dict:
        selected = drafted or self.draft_review()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        draft = result.outcome_review_draft
        assert draft is not None
        values = [
            "record-review",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--draft-id",
            draft.draft_id,
            "--draft-hash",
            draft.content_hash,
            "--evaluation-hash",
            result.evaluation_hash,
            "--reviewed-hypothesis-id",
            draft.reviewed_hypothesis_id,
            "--human-actor",
            "human:reviewer-1",
            "--reviewed-at",
            REVIEWED_AT,
            "--decision",
            ReviewDecision.APPROVED.value,
            "--diagnoses-file",
            str(self.diagnoses_file(draft.reviewed_hypothesis_id)),
            "--evidence-reference-id",
            draft.reviewed_hypothesis_id,
            "--notes",
            "Human reviewed the deterministic result and advisory diagnosis.",
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def draft_lesson(self, reviewed: dict | None = None, *, commit: bool = True) -> dict:
        selected = reviewed or self.record_review()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        values = [
            "draft-lesson",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--forecast-id",
            result.forecast_id,
            "--forecast-hash",
            result.forecast_hash,
            "--evaluation-id",
            result.evaluation_id,
            "--evaluation-hash",
            result.evaluation_hash,
            "--review-id",
            result.human_review_id,
            "--review-hash",
            result.human_review_hash,
            "--proposed-at",
            PROPOSED_AT,
            "--provider",
            "openai",
            "--model",
            "fixture-v1",
            "--shadow",
            "--allow-model-call",
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def record_lesson(self, drafted: dict | None = None, *, commit: bool = True) -> dict:
        selected = drafted or self.draft_lesson()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        draft = result.mistake_lesson_proposal_draft
        assert draft is not None
        values = [
            "record-lesson",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--draft-id",
            draft.draft_id,
            "--draft-hash",
            draft.content_hash,
            "--confirm-draft-hash",
            draft.content_hash,
            "--human-actor",
            "human:lesson-reviewer-1",
            "--recorded-at",
            LESSON_RECORDED_AT,
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)

    def activate(
        self,
        recorded: dict | None = None,
        *,
        scope_type: LessonScopeType = LessonScopeType.EXACT_CASE,
        commit: bool = True,
    ) -> dict:
        selected = recorded or self.record_lesson()
        result = ShadowWorkflowResult.from_dict(selected["result"])
        lesson = selected["lesson"]
        values = [
            "activate-lesson",
            result.workflow_id,
            "--checkpoint-hash",
            selected["checkpoint"]["content_hash"],
            "--lesson-id",
            lesson["lesson_id"],
            "--lesson-hash",
            lesson["content_hash"],
            "--confirm-lesson-id",
            lesson["lesson_id"],
            "--scope-type",
            scope_type.value,
            "--human-actor",
            "human:lesson-activator-1",
            "--reason",
            "Human verified the exact-case evidence threshold.",
            "--recorded-at",
            ACTIVATED_AT,
        ]
        if commit:
            values.append("--commit")
        return self.execute(values)


class Phase11ManualCliTests(Phase11ManualCliFixture):
    def test_existing_cli_and_all_additive_commands_parse(self) -> None:
        self.assertEqual(self.parser.parse_args(["stats"]).command, "stats")
        help_text = self.parser.format_help()
        self.assertIn("phase11-shadow", help_text)
        commands = {
            "start",
            "approve-forecast",
            "register-observations",
            "evaluate",
            "draft-review",
            "record-review",
            "draft-lesson",
            "record-lesson",
            "activate-lesson",
            "status",
            "verify",
        }
        phase_action = next(
            action
            for action in self.parser._actions
            if getattr(action, "choices", None) and "phase11-shadow" in action.choices
        )
        phase_parser = phase_action.choices["phase11-shadow"]
        command_action = next(
            action for action in phase_parser._actions if getattr(action, "choices", None)
        )
        self.assertEqual(set(command_action.choices), commands)

    def test_main_dispatch_emits_json_without_changing_existing_cli(self) -> None:
        started = self.start()
        output = io.StringIO()
        with redirect_stdout(output):
            main(
                [
                    "--database",
                    str(self.database),
                    "phase11-shadow",
                    "status",
                    started["workflow_id"],
                ]
            )
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["command"], "status")
        self.assertEqual(rendered["workflow_id"], started["workflow_id"])
        self.assertFalse(rendered["provider_called"])

    def test_start_requires_explicit_inputs_and_shadow(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["phase11-shadow", "start"])
        values = self.start_arguments()
        values.remove("--shadow")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.execute(values)

    def test_start_without_model_permission_is_plan_only(self) -> None:
        result = self.execute(self.start_arguments(allow=False, commit=True))
        self.assertEqual(result["status"], "validated_plan")
        self.assertFalse(result["provider_called"])
        self.assertEqual(self.provider.calls, [])
        self.assertFalse(self.checkpoint_root.exists())
        self.assertEqual(self.store.list_forecast_agent_orchestrations(), [])

    def test_model_dry_run_calls_provider_but_writes_nothing(self) -> None:
        result = self.execute(self.start_arguments(allow=True, commit=False))
        self.assertEqual(result["status"], "dry_run_completed")
        self.assertTrue(result["provider_called"])
        self.assertEqual(len(self.provider.calls), 4)
        self.assertFalse(self.checkpoint_root.exists())
        self.assertEqual(self.store.list_forecast_agent_orchestrations(), [])
        self.assertEqual(self.store.list_forecast_records(), [])

    def test_start_uses_explicit_provider_model_and_repeats_idempotently(self) -> None:
        first = self.start()
        call_count = len(self.provider.calls)
        second = self.execute(self.start_arguments())
        self.assertEqual(self.provider_settings, [("openai", "fixture-v1")])
        self.assertEqual(len(self.provider.calls), call_count)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(second["status"], "idempotent_replay")
        self.assertFalse(second["database_writes"])
        self.assertFalse(second["checkpoint_writes"])
        self.assertEqual(
            first["checkpoint"]["content_hash"],
            second["checkpoint"]["content_hash"],
        )

    def test_approval_requires_exact_hash_and_rejects_hard_invalid_candidate(self) -> None:
        started = self.start()
        result = ShadowWorkflowResult.from_dict(started["result"])
        proposal = result.proposal
        assert proposal is not None
        bad = self.approve(started, commit=False)
        self.assertTrue(bad["dry_run"])
        self.assertEqual(self.store.list_forecast_records(), [])
        values = [
            "approve-forecast",
            result.workflow_id,
            "--checkpoint-hash",
            started["checkpoint"]["content_hash"],
            "--proposal-hash",
            proposal.content_hash,
            "--confirm-proposal-hash",
            "0" * 64,
            "--selected-candidate-id",
            proposal.proposed_selected_candidate_id,
            "--human-actor",
            "human:test",
            "--approved-at",
            APPROVED_AT,
            "--direction",
            "up",
            "--claims-file",
            str(self.claims_file(started)),
            "--approval-note",
            "Explicit mismatch test.",
            "--commit",
        ]
        with self.assertRaises(ValueError):
            self.execute(values)

        other = Phase11ManualCliFixture()
        other.setUp()
        self.addCleanup(other.doCleanups)
        other.provider = CompositeFixtureProvider(
            d1=D1FixtureProvider(
                hard_invalid_roles={TechnicalAgentRole.PRIMARY_WAVE_COUNTER.value}
            )
        )
        invalid_start = other.start()
        invalid_result = ShadowWorkflowResult.from_dict(invalid_start["result"])
        invalid_proposal = invalid_result.proposal
        assert invalid_proposal is not None
        invalid_candidate = invalid_proposal.ineligible_candidate_ids[0]
        with self.assertRaises(ValueError):
            other.approve(invalid_start, candidate_id=invalid_candidate)

    def test_stale_or_conflicting_transition_is_rejected(self) -> None:
        started = self.start()
        self.approve(started)
        claims = json.loads(self.claims_file(started).read_text(encoding="utf-8"))
        claims["target_claims"][0]["target_high"] = 146.0
        conflicting = self.write_json("conflicting-claims.json", claims)
        result = ShadowWorkflowResult.from_dict(started["result"])
        proposal = result.proposal
        assert proposal is not None
        values = [
            "approve-forecast",
            result.workflow_id,
            "--checkpoint-hash",
            started["checkpoint"]["content_hash"],
            "--proposal-hash",
            proposal.content_hash,
            "--confirm-proposal-hash",
            proposal.content_hash,
            "--selected-candidate-id",
            proposal.proposed_selected_candidate_id,
            "--human-actor",
            "human:other",
            "--approved-at",
            APPROVED_AT,
            "--direction",
            "up",
            "--claims-file",
            str(conflicting),
            "--approval-note",
            "Conflicting branch.",
            "--commit",
        ]
        with self.assertRaises(ValueError):
            self.execute(values)

    def test_observation_commit_gate_and_horizon_validation(self) -> None:
        approved = self.approve()
        dry = self.register(approved, commit=False)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.store.list_forecast_observation_sets(), [])
        path = self.observation_file(approved)
        observation = ForecastObservationSet.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
        payload = observation.to_dict()
        payload.pop("content_hash")
        payload["horizon_end_utc"] = "2026-01-11T21:00:00+00:00"
        incompatible = ForecastObservationSet.create(**payload)
        bad_file = self.write_json("bad-observations.json", incompatible.to_dict())
        result = ShadowWorkflowResult.from_dict(approved["result"])
        with self.assertRaises(ValueError):
            self.execute(
                [
                    "register-observations",
                    result.workflow_id,
                    "--checkpoint-hash",
                    approved["checkpoint"]["content_hash"],
                    "--observation-file",
                    str(bad_file),
                    "--accepted-at",
                    OBSERVED_AT,
                    "--commit",
                ]
            )

    def test_evaluation_is_deterministic_and_never_calls_provider(self) -> None:
        registered = self.register()
        calls = len(self.provider.calls)
        dry = self.evaluate(registered, commit=False)
        self.assertEqual(len(self.provider.calls), calls)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.store.list_forecast_outcome_evaluations(), [])
        committed = self.evaluate(registered)
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(committed["workflow_stage"], "deterministic_evaluation")

    def test_review_draft_needs_permission_exact_refs_and_commit(self) -> None:
        evaluated = self.evaluate()
        result = ShadowWorkflowResult.from_dict(evaluated["result"])
        base = [
            "draft-review",
            result.workflow_id,
            "--checkpoint-hash",
            evaluated["checkpoint"]["content_hash"],
            "--forecast-id",
            result.forecast_id,
            "--forecast-hash",
            result.forecast_hash,
            "--observation-set-id",
            result.observation_set_id,
            "--observation-set-hash",
            result.observation_set_hash,
            "--evaluation-id",
            result.evaluation_id,
            "--evaluation-hash",
            result.evaluation_hash,
            "--drafted-at",
            DRAFTED_AT,
            "--provider",
            "openai",
            "--model",
            "fixture-v1",
            "--shadow",
        ]
        calls = len(self.provider.calls)
        plan = self.execute(base)
        self.assertEqual(plan["status"], "validated_plan")
        self.assertEqual(len(self.provider.calls), calls)
        dry = self.execute([*base, "--allow-model-call"])
        self.assertEqual(len(self.provider.calls), calls + 1)
        self.assertFalse(dry["database_writes"])
        self.assertEqual(
            self.store.list_forecast_outcome_learning_orchestrations(), []
        )
        bad = list(base)
        bad[bad.index(result.forecast_hash)] = "0" * 64
        with self.assertRaises(ValueError):
            self.execute([*bad, "--allow-model-call", "--commit"])

    def test_human_review_requires_diagnosis_evidence_hashes_and_commit(self) -> None:
        drafted = self.draft_review()
        result = ShadowWorkflowResult.from_dict(drafted["result"])
        draft = result.outcome_review_draft
        assert draft is not None
        dry = self.record_review(drafted, commit=False)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.store.list_forecast_outcome_reviews(), [])
        values = [
            "record-review",
            result.workflow_id,
            "--checkpoint-hash",
            drafted["checkpoint"]["content_hash"],
            "--draft-id",
            draft.draft_id,
            "--draft-hash",
            "0" * 64,
            "--evaluation-hash",
            result.evaluation_hash,
            "--reviewed-hypothesis-id",
            draft.reviewed_hypothesis_id,
            "--human-actor",
            "human:test",
            "--reviewed-at",
            REVIEWED_AT,
            "--decision",
            "approved",
            "--diagnoses-file",
            str(self.diagnoses_file(draft.reviewed_hypothesis_id)),
            "--notes",
            "Hash mismatch.",
            "--commit",
        ]
        with self.assertRaises(ValueError):
            self.execute(values)

    def test_lesson_draft_needs_approved_review_permission_and_commit(self) -> None:
        reviewed = self.record_review()
        result = ShadowWorkflowResult.from_dict(reviewed["result"])
        base = [
            "draft-lesson",
            result.workflow_id,
            "--checkpoint-hash",
            reviewed["checkpoint"]["content_hash"],
            "--forecast-id",
            result.forecast_id,
            "--forecast-hash",
            result.forecast_hash,
            "--evaluation-id",
            result.evaluation_id,
            "--evaluation-hash",
            result.evaluation_hash,
            "--review-id",
            result.human_review_id,
            "--review-hash",
            result.human_review_hash,
            "--proposed-at",
            PROPOSED_AT,
            "--provider",
            "openai",
            "--model",
            "fixture-v1",
            "--shadow",
        ]
        calls = len(self.provider.calls)
        plan = self.execute(base)
        self.assertFalse(plan["provider_called"])
        self.assertEqual(len(self.provider.calls), calls)
        dry = self.execute([*base, "--allow-model-call"])
        self.assertTrue(dry["human_action_required"])
        self.assertFalse(dry["database_writes"])
        self.assertEqual(len(self.provider.calls), calls + 1)

    def test_lesson_recording_and_activation_are_separate_human_gates(self) -> None:
        drafted = self.draft_lesson()
        result = ShadowWorkflowResult.from_dict(drafted["result"])
        draft = result.mistake_lesson_proposal_draft
        assert draft is not None
        with self.assertRaises(ValueError):
            self.execute(
                [
                    "record-lesson",
                    result.workflow_id,
                    "--checkpoint-hash",
                    drafted["checkpoint"]["content_hash"],
                    "--draft-id",
                    draft.draft_id,
                    "--draft-hash",
                    draft.content_hash,
                    "--confirm-draft-hash",
                    "0" * 64,
                    "--human-actor",
                    "human:test",
                    "--recorded-at",
                    LESSON_RECORDED_AT,
                    "--commit",
                ]
            )
        recorded = self.record_lesson(drafted)
        self.assertEqual(recorded["lesson_status"], LessonStatus.PROPOSED.value)
        self.assertEqual(recorded["checkpoint"]["stage"], "lesson_recorded")
        lesson_id = recorded["lesson"]["lesson_id"]
        events = self.store.list_mistake_memory_events(
            lesson_id=lesson_id, limit=100
        )
        self.assertFalse(any(item.to_status is LessonStatus.ACTIVE for item in events))
        with self.assertRaises(ValueError):
            self.activate(
                recorded, scope_type=LessonScopeType.GENERAL, commit=False
            )
        dry = self.activate(recorded, commit=False)
        self.assertTrue(dry["dry_run"])
        committed = self.activate(recorded)
        self.assertEqual(committed["lesson_status"], LessonStatus.ACTIVE.value)
        repeat = self.activate(recorded)
        self.assertTrue(repeat["idempotent_replay"])

    def test_status_and_verify_are_read_only_and_provider_free(self) -> None:
        activated = self.activate()
        workflow_id = activated["workflow_id"]
        file_count = len(tuple(self.checkpoint_root.glob("*.json")))
        calls = len(self.provider.calls)
        connection = sqlite3.connect(self.database)
        try:
            before_data_version = connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            status = self.execute(["status", workflow_id])
            verified = self.execute(["verify", workflow_id])
            after_data_version = connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(status["durable_stage"], "lesson_activated")
        self.assertEqual(status["lesson"]["status"], LessonStatus.ACTIVE.value)
        self.assertTrue(verified["valid"], verified["errors"])
        self.assertEqual(verified["checks"]["sqlite_integrity"], "ok")
        self.assertEqual(verified["checks"]["foreign_key_violations"], 0)
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(len(tuple(self.checkpoint_root.glob("*.json"))), file_count)
        self.assertEqual(before_data_version, after_data_version)

    def test_checkpoints_are_frozen_append_only_tamper_evident_and_path_safe(self) -> None:
        started = self.start()
        checkpoint = ManualShadowWorkflowCheckpoint.from_dict(started["checkpoint"])
        with self.assertRaises(FrozenInstanceError):
            checkpoint.stage = "changed"  # type: ignore[misc]
        store = Phase11CheckpointStore(self.checkpoint_root)
        stored, inserted = store.commit(checkpoint)
        self.assertFalse(inserted)
        self.assertEqual(stored.content_hash, checkpoint.content_hash)
        self.assertEqual(len(tuple(self.checkpoint_root.glob("*.json"))), 1)
        with self.assertRaises(ValueError):
            store.list("../outside")
        path = next(self.checkpoint_root.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["recorded_at_utc"] = "2026-01-05T21:05:01+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.list(checkpoint.workflow_id)

    def test_no_schema_fetch_scheduler_trading_or_production_side_effects(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "elliott_ai" / "phase11_cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            imported.isdisjoint(
                {"requests", "httpx", "telegram", "tradingview", "websocket"}
            )
        )
        self.start()
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertFalse(any("shadow_workflow" in item for item in tables))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()
        self.assertTrue(str(self.database).startswith(self.temporary.name))


if __name__ == "__main__":
    unittest.main()
