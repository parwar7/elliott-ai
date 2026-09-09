"""Manual, default-off command interface for the Phase 11 shadow workflow.

This module deliberately has no scheduler, market-data fetcher, trading action,
or active-analysis hook. Model calls and persistence are separate explicit
permissions, and durable workflow state lives in immutable JSON checkpoints.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .config import Settings
from .forecast_outcomes import (
    ForecastObservationSet,
    ForecastOutcomeEvaluation,
    forecast_observation_set_content_hash,
    forecast_outcome_evaluation_content_hash,
    validate_forecast_observation_set,
    validate_forecast_outcome_evaluation,
)
from .forecast_records import (
    ConfirmationClaim,
    ExpectedCompletionWindow,
    ForecastDirection,
    InvalidationClaim,
    TargetClaim,
    canonical_forecast_json,
    canonical_sha256,
    stored_record_content_hash,
    validate_forecast_record,
)
from .knowledge import KnowledgeStore
from .mistake_memory import (
    FailureDiagnosis,
    ForecastOutcomeReview,
    LessonScopeType,
    LessonStatus,
    MistakeMemoryLesson,
    ReviewDecision,
    approve_lesson,
    create_forecast_outcome_review,
    forecast_outcome_review_content_hash,
    lesson_current_status,
    propose_mistake_memory_lesson,
    validate_forecast_outcome_review,
    validate_lesson_event_chain,
)
from .outcome_learning_agents import (
    OutcomeLearningAgentOrchestrator,
    validate_outcome_learning_orchestration,
)
from .phase11_shadow_workflow import (
    OperatorApproval,
    Phase11ShadowWorkflow,
    ShadowWorkflowResult,
    ShadowWorkflowStage,
    replay_shadow_workflow_result,
    validate_shadow_workflow_result,
)
from .providers import AnalysisProvider, create_provider
from .technical_agent_orchestrator import (
    TechnicalAgentOrchestrator,
    build_technical_agent_request_from_stored_records,
    validate_technical_agent_orchestration,
)


PHASE11_CLI_POLICY_VERSION = "phase11e2-manual-shadow-policy-1.0.0"
MANUAL_CHECKPOINT_SCHEMA_VERSION = "phase11-manual-checkpoint-1.0.0"
PHASE11_SHADOW_STATE_DIRECTORY = "phase11-shadow-workflows"

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
_MANUAL_STAGES = frozenset(
    {
        "human_outcome_review_recorded",
        "lesson_recorded",
        "lesson_activated",
    }
)
_OPERATIONS = frozenset(
    {
        "start",
        "approve_forecast",
        "register_observations",
        "evaluate",
        "draft_review",
        "record_review",
        "draft_lesson",
        "record_lesson",
        "activate_lesson",
    }
)


class ManualCheckpointKind(StrEnum):
    WORKFLOW_RESULT = "workflow_result"
    HUMAN_ACTION = "human_action"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_hash(value: Any, *, field_name: str) -> str:
    text = _require_text(value, field_name=field_name).lower()
    if _HASH_PATTERN.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return text


def _normalize_utc(value: Any, *, field_name: str) -> str:
    text = _require_text(value, field_name=field_name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _string_mapping(
    value: Mapping[str, str], *, field_name: str, hashes: bool = False
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        resolved_key = _require_text(key, field_name=f"{field_name} key")
        normalized[resolved_key] = (
            _require_hash(item, field_name=f"{field_name}[{resolved_key!r}]")
            if hashes
            else _require_text(item, field_name=f"{field_name}[{resolved_key!r}]")
        )
    return MappingProxyType(dict(sorted(normalized.items())))


def _safe_component(value: Any, *, field_name: str) -> str:
    text = _require_text(value, field_name=field_name)
    if _SAFE_COMPONENT.fullmatch(text) is None or ".." in text:
        raise ValueError(f"{field_name} contains an unsafe path component.")
    return text


def _checkpoint_content_hash(value: "ManualShadowWorkflowCheckpoint") -> str:
    payload = value.to_dict()
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class ManualShadowWorkflowCheckpoint:
    workflow_id: str
    sequence_number: int
    operation: str
    stage: str
    checkpoint_kind: ManualCheckpointKind
    previous_checkpoint_hash: str | None
    operation_key: str
    workflow_result: ShadowWorkflowResult
    action_ids: Mapping[str, str]
    action_hashes: Mapping[str, str]
    human_action_required: bool
    recorded_at_utc: str
    schema_version: str = MANUAL_CHECKPOINT_SCHEMA_VERSION
    policy_version: str = PHASE11_CLI_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workflow_id",
            _safe_component(self.workflow_id, field_name="workflow_id"),
        )
        if (
            not isinstance(self.sequence_number, int)
            or isinstance(self.sequence_number, bool)
            or self.sequence_number < 1
        ):
            raise ValueError("sequence_number must be a positive integer.")
        operation = _require_text(self.operation, field_name="operation")
        if operation not in _OPERATIONS:
            raise ValueError("Unsupported manual workflow operation.")
        object.__setattr__(self, "operation", operation)
        stage = _require_text(self.stage, field_name="stage")
        allowed_stages = {item.value for item in ShadowWorkflowStage} | set(
            _MANUAL_STAGES
        )
        if stage not in allowed_stages:
            raise ValueError("Unsupported manual workflow stage.")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(
            self,
            "checkpoint_kind",
            _enum_value(
                self.checkpoint_kind,
                ManualCheckpointKind,
                field_name="checkpoint_kind",
            ),
        )
        previous = (
            None
            if self.previous_checkpoint_hash is None
            else _require_hash(
                self.previous_checkpoint_hash,
                field_name="previous_checkpoint_hash",
            )
        )
        if self.sequence_number == 1 and previous is not None:
            raise ValueError("The first durable checkpoint cannot have a predecessor.")
        if self.sequence_number > 1 and previous is None:
            raise ValueError("Later durable checkpoints require a predecessor hash.")
        object.__setattr__(self, "previous_checkpoint_hash", previous)
        object.__setattr__(
            self,
            "operation_key",
            _require_hash(self.operation_key, field_name="operation_key"),
        )
        if isinstance(self.workflow_result, Mapping):
            object.__setattr__(
                self,
                "workflow_result",
                ShadowWorkflowResult.from_dict(self.workflow_result),
            )
        elif not isinstance(self.workflow_result, ShadowWorkflowResult):
            raise TypeError("workflow_result must be a ShadowWorkflowResult.")
        if self.workflow_result.workflow_id != self.workflow_id:
            raise ValueError("Checkpoint and workflow result IDs differ.")
        object.__setattr__(
            self,
            "action_ids",
            _string_mapping(self.action_ids, field_name="action_ids"),
        )
        object.__setattr__(
            self,
            "action_hashes",
            _string_mapping(
                self.action_hashes, field_name="action_hashes", hashes=True
            ),
        )
        if set(self.action_ids) != set(self.action_hashes):
            raise ValueError("Checkpoint action IDs and hashes must use identical keys.")
        if not isinstance(self.human_action_required, bool):
            raise TypeError("human_action_required must be boolean.")
        object.__setattr__(
            self,
            "recorded_at_utc",
            _normalize_utc(self.recorded_at_utc, field_name="recorded_at_utc"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _require_text(self.policy_version, field_name="policy_version"),
        )
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}

    @classmethod
    def create(cls, **values: Any) -> "ManualShadowWorkflowCheckpoint":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=_checkpoint_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ManualShadowWorkflowCheckpoint":
        raw = dict(value)
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "ManualShadowWorkflowCheckpoint contains unknown fields: "
                + ", ".join(unknown)
                + "."
            )
        result = cls(**raw)
        if verify_hash and result.content_hash != _checkpoint_content_hash(result):
            raise ValueError("Durable checkpoint content_hash does not match.")
        return result


class Phase11CheckpointStore:
    """Content-addressed, append-only JSON storage for manual workflow state."""

    def __init__(self, state_root: Path) -> None:
        self.root = Path(state_root).resolve()

    @staticmethod
    def operation_key(
        operation: str,
        *,
        previous_checkpoint_hash: str | None,
        inputs: Mapping[str, Any],
    ) -> str:
        if operation not in _OPERATIONS:
            raise ValueError("Unsupported manual workflow operation.")
        return canonical_sha256(
            {
                "operation": operation,
                "previous_checkpoint_hash": previous_checkpoint_hash,
                "inputs": _json_value(inputs),
                "policy_version": PHASE11_CLI_POLICY_VERSION,
            }
        )

    def _path(self, checkpoint: ManualShadowWorkflowCheckpoint) -> Path:
        workflow_id = _safe_component(
            checkpoint.workflow_id, field_name="workflow_id"
        )
        stage = _safe_component(checkpoint.stage, field_name="stage")
        name = (
            f"{workflow_id}__{checkpoint.sequence_number:04d}__{stage}__"
            f"{checkpoint.content_hash}.json"
        )
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Checkpoint path escapes the application state directory.")
        return path

    def _load_path(self, path: Path) -> ManualShadowWorkflowCheckpoint:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Checkpoint path escapes the application state directory.")
        value = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Checkpoint file must contain one JSON object.")
        checkpoint = ManualShadowWorkflowCheckpoint.from_dict(value)
        if resolved.name != self._path(checkpoint).name:
            raise ValueError("Checkpoint filename does not match its immutable payload.")
        return checkpoint

    def list(self, workflow_id: str) -> tuple[ManualShadowWorkflowCheckpoint, ...]:
        safe = _safe_component(workflow_id, field_name="workflow_id")
        if not self.root.exists():
            return ()
        if not self.root.is_dir():
            raise ValueError("Phase 11 checkpoint state path is not a directory.")
        checkpoints = tuple(
            self._load_path(path)
            for path in sorted(self.root.glob(f"{safe}__*.json"))
        )
        errors = self.validate_chain(checkpoints, expected_workflow_id=safe)
        if errors:
            raise ValueError("Invalid durable checkpoint chain: " + " ".join(errors))
        return checkpoints

    def latest(self, workflow_id: str) -> ManualShadowWorkflowCheckpoint:
        checkpoints = self.list(workflow_id)
        if not checkpoints:
            raise KeyError(f"No durable Phase 11 workflow exists for {workflow_id}.")
        return checkpoints[-1]

    def by_hash(
        self, workflow_id: str, content_hash: str
    ) -> ManualShadowWorkflowCheckpoint:
        expected = _require_hash(content_hash, field_name="checkpoint_hash")
        for item in self.list(workflow_id):
            if item.content_hash == expected:
                return item
        raise KeyError("The supplied checkpoint hash does not exist in this workflow.")

    def find_operation(
        self, workflow_id: str, operation_key: str
    ) -> ManualShadowWorkflowCheckpoint | None:
        expected = _require_hash(operation_key, field_name="operation_key")
        return next(
            (item for item in self.list(workflow_id) if item.operation_key == expected),
            None,
        )

    def commit(
        self, checkpoint: ManualShadowWorkflowCheckpoint
    ) -> tuple[ManualShadowWorkflowCheckpoint, bool]:
        if checkpoint.content_hash != _checkpoint_content_hash(checkpoint):
            raise ValueError("Checkpoint content hash differs before persistence.")
        existing = self.list(checkpoint.workflow_id)
        duplicate = next(
            (item for item in existing if item.operation_key == checkpoint.operation_key),
            None,
        )
        if duplicate is not None:
            if duplicate.content_hash != checkpoint.content_hash:
                raise ValueError("An identical operation key has conflicting checkpoint content.")
            return duplicate, False
        expected_sequence = len(existing) + 1
        expected_previous = existing[-1].content_hash if existing else None
        if checkpoint.sequence_number != expected_sequence:
            raise ValueError("Checkpoint sequence does not extend the current chain.")
        if checkpoint.previous_checkpoint_hash != expected_previous:
            raise ValueError("Checkpoint predecessor does not match the current chain head.")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(checkpoint)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(checkpoint.to_dict(), ensure_ascii=True, indent=2))
                handle.write("\n")
        except FileExistsError:
            stored = self._load_path(path)
            if stored.content_hash != checkpoint.content_hash:
                raise ValueError("Checkpoint filename collision has different content.")
            return stored, False
        return checkpoint, True

    @staticmethod
    def validate_chain(
        checkpoints: Sequence[ManualShadowWorkflowCheckpoint],
        *,
        expected_workflow_id: str | None = None,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        previous: ManualShadowWorkflowCheckpoint | None = None
        seen_operations: set[str] = set()
        for index, item in enumerate(checkpoints, start=1):
            if item.content_hash != _checkpoint_content_hash(item):
                errors.append(f"Checkpoint {index} content hash differs.")
            if expected_workflow_id and item.workflow_id != expected_workflow_id:
                errors.append(f"Checkpoint {index} belongs to another workflow.")
            if item.sequence_number != index:
                errors.append("Durable checkpoint sequence is not contiguous.")
            expected_previous = previous.content_hash if previous else None
            if item.previous_checkpoint_hash != expected_previous:
                errors.append(f"Checkpoint {index} predecessor hash differs.")
            if item.operation_key in seen_operations:
                errors.append("Durable checkpoint operation key is duplicated.")
            seen_operations.add(item.operation_key)
            result_errors = validate_shadow_workflow_result(item.workflow_result)
            errors.extend(
                f"Checkpoint {index} workflow result: {error}"
                for error in result_errors
            )
            if previous is not None:
                previous_result = previous.workflow_result
                current_result = item.workflow_result
                if len(current_result.lineage.checkpoints) < len(
                    previous_result.lineage.checkpoints
                ):
                    errors.append("A durable checkpoint discards prior E1 lineage.")
                if (
                    current_result.lineage.checkpoints[
                        : len(previous_result.lineage.checkpoints)
                    ]
                    != previous_result.lineage.checkpoints
                ):
                    errors.append("A durable checkpoint mutates prior E1 lineage.")
            previous = item
        return tuple(errors)


class _NoModelProvider:
    name = "phase11-no-model"
    model = None

    def generate(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("This Phase 11 command is deterministic and cannot call a model.")

    def generate_strict_json(self, **_: Any) -> dict[str, Any]:
        return self.generate()


ProviderFactory = Callable[[Settings], AnalysisProvider]
WorkflowFactory = Callable[[KnowledgeStore, AnalysisProvider], Phase11ShadowWorkflow]


def _workflow(
    store: KnowledgeStore,
    provider: AnalysisProvider,
    workflow_factory: WorkflowFactory | None,
) -> Phase11ShadowWorkflow:
    if workflow_factory is not None:
        return workflow_factory(store, provider)
    return Phase11ShadowWorkflow(
        store=store,
        technical_orchestrator=TechnicalAgentOrchestrator(
            provider=provider,
            lesson_store=store,
        ),
        outcome_learning_orchestrator=OutcomeLearningAgentOrchestrator(
            provider=provider
        ),
    )


def _load_json(path_text: str, *, expected: str) -> Any:
    path = Path(_require_text(path_text, field_name="JSON path")).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if expected == "mapping" and not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain one JSON object.")
    if expected == "sequence" and (
        isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
    ):
        raise ValueError(f"{path} must contain one JSON array.")
    return value


def _claims(path_text: str) -> tuple[
    tuple[TargetClaim, ...],
    tuple[InvalidationClaim, ...],
    tuple[ConfirmationClaim, ...],
    tuple[ExpectedCompletionWindow, ...],
]:
    raw = dict(_load_json(path_text, expected="mapping"))
    allowed = {
        "target_claims",
        "invalidation_claims",
        "confirmation_claims",
        "expected_completion_windows",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError("Claims JSON contains unknown keys: " + ", ".join(unknown) + ".")
    targets = tuple(TargetClaim.from_dict(item) for item in raw.get("target_claims", ()))
    invalidations = tuple(
        InvalidationClaim.from_dict(item)
        for item in raw.get("invalidation_claims", ())
    )
    confirmations = tuple(
        ConfirmationClaim.from_dict(item)
        for item in raw.get("confirmation_claims", ())
    )
    windows = tuple(
        ExpectedCompletionWindow.from_dict(item)
        for item in raw.get("expected_completion_windows", ())
    )
    if not targets or not invalidations or not windows:
        raise ValueError(
            "Claims JSON requires target_claims, invalidation_claims, and "
            "expected_completion_windows."
        )
    return targets, invalidations, confirmations, windows


def _diagnoses(path_text: str) -> tuple[FailureDiagnosis, ...]:
    raw = _load_json(path_text, expected="sequence")
    return tuple(FailureDiagnosis.from_dict(item) for item in raw)


def _checkpoint_root(settings: Settings) -> Path:
    return settings.database_path.parent / PHASE11_SHADOW_STATE_DIRECTORY


def _explicit_provider_settings(
    settings: Settings, *, provider: str, model: str
) -> Settings:
    """Bind a model call to the operator-confirmed provider configuration."""

    return replace(settings, provider=provider, model=model)


def _plan(
    command: str,
    *,
    inputs: Mapping[str, Any],
    model_call_required: bool,
    allow_model_call: bool,
    commit: bool,
) -> dict[str, Any]:
    return {
        "command": command,
        "status": "validated_plan",
        "dry_run": True,
        "model_call_required": model_call_required,
        "model_call_allowed": allow_model_call,
        "provider_called": False,
        "commit_requested": commit,
        "database_writes": False,
        "checkpoint_writes": False,
        "inputs": _json_value(inputs),
        "next_action": (
            "Repeat with --allow-model-call to execute the explicitly configured provider."
            if model_call_required and not allow_model_call
            else "Validation completed; no persistent state was changed."
        ),
    }


def _checkpoint_response(
    command: str,
    result: ShadowWorkflowResult,
    *,
    checkpoint: ManualShadowWorkflowCheckpoint | None,
    inserted: bool,
    provider_called: bool,
) -> dict[str, Any]:
    return {
        "command": command,
        "status": (
            "dry_run_completed"
            if checkpoint is None
            else ("committed" if inserted else "idempotent_replay")
        ),
        "dry_run": checkpoint is None,
        "provider_called": provider_called,
        "database_writes": bool(checkpoint is not None and inserted),
        "checkpoint_writes": bool(checkpoint is not None and inserted),
        "idempotent_replay": bool(checkpoint is not None and not inserted),
        "workflow_id": result.workflow_id,
        "workflow_stage": result.current_stage.value,
        "workflow_status": result.status.value,
        "human_action_required": (
            checkpoint.human_action_required
            if checkpoint is not None
            else bool(result.human_gate)
        ),
        "pending_human_action": (
            result.human_gate.required_action if result.human_gate else None
        ),
        "checkpoint": checkpoint.to_dict() if checkpoint else None,
        "result": result.to_dict(),
    }


def _operation_inputs(**values: Any) -> Mapping[str, Any]:
    return _freeze_json(values)


def _prepare_transition(
    checkpoints: Phase11CheckpointStore,
    *,
    workflow_id: str,
    operation: str,
    predecessor_hash: str | None,
    inputs: Mapping[str, Any],
) -> tuple[
    ManualShadowWorkflowCheckpoint | None,
    ManualShadowWorkflowCheckpoint | None,
    str,
]:
    operation_key = checkpoints.operation_key(
        operation,
        previous_checkpoint_hash=predecessor_hash,
        inputs=inputs,
    )
    repeated = checkpoints.find_operation(workflow_id, operation_key)
    if repeated is not None:
        return None, repeated, operation_key
    chain = checkpoints.list(workflow_id)
    if predecessor_hash is None:
        if chain:
            raise ValueError("A conflicting start already exists for this workflow.")
        return None, None, operation_key
    predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
    if not chain or chain[-1].content_hash != predecessor.content_hash:
        raise ValueError("The supplied checkpoint is stale; branching is forbidden.")
    child = next(
        (
            item
            for item in chain
            if item.previous_checkpoint_hash == predecessor.content_hash
        ),
        None,
    )
    if child is not None:
        raise ValueError("A conflicting transition already extends this checkpoint.")
    return predecessor, None, operation_key


def _new_checkpoint(
    *,
    previous: ManualShadowWorkflowCheckpoint | None,
    operation: str,
    operation_key: str,
    result: ShadowWorkflowResult,
    recorded_at_utc: str,
    stage: str | None = None,
    checkpoint_kind: ManualCheckpointKind = ManualCheckpointKind.WORKFLOW_RESULT,
    action_ids: Mapping[str, str] | None = None,
    action_hashes: Mapping[str, str] | None = None,
    human_action_required: bool | None = None,
) -> ManualShadowWorkflowCheckpoint:
    return ManualShadowWorkflowCheckpoint.create(
        workflow_id=result.workflow_id,
        sequence_number=(previous.sequence_number + 1 if previous else 1),
        operation=operation,
        stage=stage or result.current_stage.value,
        checkpoint_kind=checkpoint_kind,
        previous_checkpoint_hash=(previous.content_hash if previous else None),
        operation_key=operation_key,
        workflow_result=result,
        action_ids=action_ids or {},
        action_hashes=action_hashes or {},
        human_action_required=(
            bool(result.human_gate)
            if human_action_required is None
            else human_action_required
        ),
        recorded_at_utc=recorded_at_utc,
    )


def _repeated_response(
    command: str, checkpoint: ManualShadowWorkflowCheckpoint
) -> dict[str, Any]:
    return _checkpoint_response(
        command,
        checkpoint.workflow_result,
        checkpoint=checkpoint,
        inserted=False,
        provider_called=False,
    )


def _exact_previous(
    checkpoint: ManualShadowWorkflowCheckpoint,
    *,
    expected_stage: str,
) -> ShadowWorkflowResult:
    if checkpoint.stage != expected_stage:
        raise ValueError(
            f"Command requires durable stage {expected_stage}; found {checkpoint.stage}."
        )
    return replay_shadow_workflow_result(checkpoint.workflow_result)


def _review_bundle(
    store: KnowledgeStore, review_id: str
) -> tuple[Any, ForecastOutcomeEvaluation, ForecastOutcomeReview]:
    review = store.get_forecast_outcome_review(review_id)
    if review is None:
        raise KeyError(f"Outcome review {review_id} does not exist.")
    evaluation = store.get_forecast_outcome_evaluation(review.evaluation_id)
    forecast = store.get_forecast_record(review.forecast_id)
    if evaluation is None or forecast is None:
        raise KeyError("Review forecast/evaluation lineage is incomplete.")
    errors = validate_forecast_outcome_review(review, evaluation=evaluation)
    errors = tuple(item for item in errors if "initial review" not in item.casefold())
    if errors:
        raise ValueError("Outcome review failed immutable validation: " + " ".join(errors))
    return forecast, evaluation, review


def _active_lessons_at(
    store: KnowledgeStore, cutoff_utc: str
) -> tuple[MistakeMemoryLesson, ...]:
    cutoff = _normalize_utc(cutoff_utc, field_name="lesson_memory_cutoff")
    active: list[MistakeMemoryLesson] = []
    for lesson in store.list_mistake_memory_lessons(limit=10_000):
        events = tuple(
            item
            for item in store.list_mistake_memory_events(
                lesson_id=lesson.lesson_id, limit=10_000
            )
            if _normalize_utc(
                item.recorded_at_utc, field_name="lesson_event.recorded_at_utc"
            )
            <= cutoff
        )
        if validate_lesson_event_chain(lesson, events):
            continue
        if (
            lesson_current_status(lesson, events, at_or_before_utc=cutoff)
            is LessonStatus.ACTIVE
        ):
            active.append(lesson)
    return tuple(sorted(active, key=lambda item: item.lesson_id))


def _build_status(
    store: KnowledgeStore,
    chain: Sequence[ManualShadowWorkflowCheckpoint],
) -> dict[str, Any]:
    latest = chain[-1]
    result = latest.workflow_result
    forecast = store.get_forecast_record(result.forecast_id) if result.forecast_id else None
    evaluation = (
        store.get_forecast_outcome_evaluation(result.evaluation_id)
        if result.evaluation_id
        else None
    )
    review = (
        store.get_forecast_outcome_review(result.human_review_id)
        if result.human_review_id
        else None
    )
    lesson_checkpoint = next(
        (item for item in reversed(chain) if "lesson" in item.action_ids), None
    )
    lesson = None
    lesson_status = None
    if lesson_checkpoint is not None:
        lesson = store.get_mistake_memory_lesson(lesson_checkpoint.action_ids["lesson"])
        if lesson is not None:
            events = store.list_mistake_memory_events(
                lesson_id=lesson.lesson_id, limit=10_000
            )
            lesson_status = lesson_current_status(lesson, events).value
    pending = result.human_gate.required_action if result.human_gate else None
    if latest.stage == "lesson_recorded":
        pending = "activate_lesson"
    elif latest.stage == "lesson_activated":
        pending = None
    elif latest.stage == "human_outcome_review_recorded":
        pending = None
    return {
        "workflow_id": result.workflow_id,
        "durable_stage": latest.stage,
        "workflow_stage": result.current_stage.value,
        "workflow_status": result.status.value,
        "checkpoint_sequence": latest.sequence_number,
        "checkpoint_hash": latest.content_hash,
        "pending_human_action": pending,
        "source_ids": dict(result.lineage.artifact_ids),
        "source_hashes": dict(result.lineage.artifact_hashes),
        "forecast": (
            {"id": forecast.forecast_id, "hash": forecast.content_hash}
            if forecast
            else None
        ),
        "evaluation": (
            {
                "id": evaluation.evaluation_id,
                "hash": evaluation.content_hash,
                "outcome_status": evaluation.outcome_status.value,
            }
            if evaluation
            else None
        ),
        "review": (
            {
                "id": review.review_id,
                "hash": review.content_hash,
                "decision": review.decision.value,
            }
            if review
            else None
        ),
        "lesson": (
            {
                "id": lesson.lesson_id,
                "hash": lesson.content_hash,
                "status": lesson_status,
            }
            if lesson
            else None
        ),
        "warnings": list(result.warnings),
        "errors": list(result.errors),
        "hidden_reasoning_stored": False,
    }


def _verify_database_relationships(
    store: KnowledgeStore,
    chain: Sequence[ManualShadowWorkflowCheckpoint],
) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    latest = chain[-1]
    result = latest.workflow_result
    proposal = result.proposal
    if proposal is not None:
        run = store.get_run(proposal.source_analysis_run_id)
        resolution = store.get_degree_resolution(proposal.source_degree_resolution_id)
        if run is None or resolution is None:
            errors.append("Proposal source analysis run or degree resolution is missing.")
        else:
            if stored_record_content_hash(run) != proposal.source_analysis_run_hash:
                errors.append("Proposal analysis-run hash differs.")
            if stored_record_content_hash(resolution) != proposal.source_degree_resolution_hash:
                errors.append("Proposal degree-resolution hash differs.")
        orchestration = store.get_forecast_agent_orchestration(
            proposal.technical_orchestration_id
        )
        if orchestration is None:
            errors.append("Proposal technical orchestration is missing.")
        elif orchestration.content_hash != proposal.technical_orchestration_hash:
            errors.append("Proposal technical orchestration hash differs.")
        else:
            errors.extend(validate_technical_agent_orchestration(orchestration))
    for artifact_name in (
        "outcome_reviewer_orchestration",
        "mistake_proposal_orchestration",
    ):
        orchestration_id = result.lineage.artifact_ids.get(artifact_name)
        orchestration_hash = result.lineage.artifact_hashes.get(artifact_name)
        if orchestration_id is None:
            continue
        orchestration = store.get_forecast_outcome_learning_orchestration(
            orchestration_id
        )
        if orchestration is None:
            errors.append(f"{artifact_name} is missing from persistence.")
        elif orchestration.content_hash != orchestration_hash:
            errors.append(f"{artifact_name} hash differs from workflow lineage.")
        else:
            errors.extend(validate_outcome_learning_orchestration(orchestration))
    forecast = store.get_forecast_record(result.forecast_id) if result.forecast_id else None
    if result.forecast_id:
        if forecast is None or forecast.content_hash != result.forecast_hash:
            errors.append("Workflow forecast reference is missing or hash-mismatched.")
        elif validate_forecast_record(forecast):
            errors.extend(validate_forecast_record(forecast))
    observation = (
        store.get_forecast_observation_set(result.observation_set_id)
        if result.observation_set_id
        else None
    )
    if result.observation_set_id:
        if observation is None or observation.content_hash != result.observation_set_hash:
            errors.append("Workflow observation reference is missing or hash-mismatched.")
        elif forecast is None:
            errors.append("Observation cannot be checked without its forecast.")
        else:
            errors.extend(validate_forecast_observation_set(observation, forecast))
            if observation.content_hash != forecast_observation_set_content_hash(observation):
                errors.append("Observation-set canonical hash differs.")
    evaluation = (
        store.get_forecast_outcome_evaluation(result.evaluation_id)
        if result.evaluation_id
        else None
    )
    if result.evaluation_id:
        if evaluation is None or evaluation.content_hash != result.evaluation_hash:
            errors.append("Workflow evaluation reference is missing or hash-mismatched.")
        elif forecast is None or observation is None:
            errors.append("Evaluation cannot be checked without forecast and observations.")
        else:
            errors.extend(
                validate_forecast_outcome_evaluation(
                    evaluation,
                    forecast=forecast,
                    observation_set=observation,
                )
            )
            if evaluation.content_hash != forecast_outcome_evaluation_content_hash(
                evaluation
            ):
                errors.append("Outcome-evaluation canonical hash differs.")
    if result.human_review_id:
        review = store.get_forecast_outcome_review(result.human_review_id)
        if review is None or review.content_hash != result.human_review_hash:
            errors.append("Workflow human-review reference is missing or hash-mismatched.")
        elif evaluation is None:
            errors.append("Human review cannot be checked without its evaluation.")
        else:
            parent = (
                store.get_forecast_outcome_review(review.parent_review_id)
                if review.parent_review_id
                else None
            )
            errors.extend(
                validate_forecast_outcome_review(
                    review,
                    evaluation=evaluation,
                    parent_review=parent,
                )
            )
    action_review_ids = {
        item.action_ids["human_review"]
        for item in chain
        if "human_review" in item.action_ids
    }
    for review_id in sorted(action_review_ids):
        review = store.get_forecast_outcome_review(review_id)
        if review is None:
            errors.append(f"Manual checkpoint review {review_id} is missing.")
            continue
        evaluation_for_review = store.get_forecast_outcome_evaluation(
            review.evaluation_id
        )
        if evaluation_for_review is None:
            errors.append(f"Manual checkpoint review {review_id} lacks its evaluation.")
            continue
        parent = (
            store.get_forecast_outcome_review(review.parent_review_id)
            if review.parent_review_id
            else None
        )
        errors.extend(
            validate_forecast_outcome_review(
                review,
                evaluation=evaluation_for_review,
                parent_review=parent,
            )
        )
        for checkpoint in chain:
            if checkpoint.action_ids.get("human_review") == review_id and (
                checkpoint.action_hashes.get("human_review")
                != review.content_hash
            ):
                errors.append(
                    f"Manual checkpoint review {review_id} hash differs."
                )
    lesson_ids = {
        item.action_ids["lesson"]
        for item in chain
        if "lesson" in item.action_ids
    }
    for lesson_id in sorted(lesson_ids):
        lesson = store.get_mistake_memory_lesson(lesson_id)
        if lesson is None:
            errors.append(f"Manual checkpoint lesson {lesson_id} is missing.")
            continue
        events = store.list_mistake_memory_events(lesson_id=lesson_id, limit=10_000)
        sources = store.list_mistake_memory_sources(lesson_id=lesson_id, limit=10_000)
        errors.extend(validate_lesson_event_chain(lesson, events))
        source_ids = {item.source_id for item in sources}
        if source_ids != set(lesson.supporting_source_ids) | set(
            lesson.counterexample_source_ids
        ):
            errors.append(f"Lesson {lesson_id} source identity set differs.")
        if any(event.to_status is LessonStatus.ACTIVE for event in events):
            if lesson_current_status(lesson, events) is not LessonStatus.ACTIVE:
                errors.append(f"Lesson {lesson_id} activation lineage is invalid.")
        for checkpoint in chain:
            if checkpoint.action_ids.get("lesson") == lesson_id and (
                checkpoint.action_hashes.get("lesson") != lesson.content_hash
            ):
                errors.append(f"Manual checkpoint lesson {lesson_id} hash differs.")
    for checkpoint in chain:
        for action_name, action_id in checkpoint.action_ids.items():
            expected_hash = checkpoint.action_hashes[action_name]
            if action_name.startswith("lesson_source:"):
                record = store.get_mistake_memory_source(action_id)
            elif action_name in {"proposal_event", "activation_event"}:
                record = store.get_mistake_memory_event(action_id)
            else:
                continue
            if record is None:
                errors.append(
                    f"Manual checkpoint action {action_name} ({action_id}) is missing."
                )
            elif record.content_hash != expected_hash:
                errors.append(
                    f"Manual checkpoint action {action_name} ({action_id}) hash differs."
                )
    connection = sqlite3.connect(
        f"file:{store.database_path.as_posix()}?mode=ro", uri=True
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if integrity != "ok":
        errors.append(f"SQLite integrity_check returned {integrity}.")
    if foreign_keys:
        errors.append(f"SQLite has {len(foreign_keys)} foreign-key violation(s).")
    return errors, warnings, {
        "sqlite_integrity": integrity,
        "foreign_key_violations": len(foreign_keys),
    }


def verify_phase11_shadow_workflow(
    store: KnowledgeStore,
    checkpoints: Phase11CheckpointStore,
    workflow_id: str,
) -> dict[str, Any]:
    chain = checkpoints.list(workflow_id)
    if not chain:
        raise KeyError(f"No durable Phase 11 workflow exists for {workflow_id}.")
    chain_errors = list(
        checkpoints.validate_chain(chain, expected_workflow_id=workflow_id)
    )
    relationship_errors, warnings, database = _verify_database_relationships(
        store, chain
    )
    errors = [*chain_errors, *relationship_errors]
    return {
        "workflow_id": workflow_id,
        "valid": not errors,
        "checkpoint_count": len(chain),
        "latest_checkpoint_hash": chain[-1].content_hash,
        "latest_stage": chain[-1].stage,
        "checks": {
            "checkpoint_lineage": "passed" if not chain_errors else "failed",
            "canonical_hashes": "passed" if not chain_errors else "failed",
            "source_relationships": "passed" if not relationship_errors else "failed",
            "cutoff_and_future_leakage": "passed" if not relationship_errors else "failed",
            "lesson_activation_lineage": "passed" if not relationship_errors else "failed",
            **database,
        },
        "errors": errors,
        "warnings": warnings,
        "provider_called": False,
        "database_writes": False,
        "checkpoint_writes": False,
    }


def _add_commit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist validated records and an immutable checkpoint.",
    )


def _add_model_gate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=("openai", "ollama"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--shadow", action="store_true", required=True)
    parser.add_argument("--allow-model-call", action="store_true")


def _add_transition(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("workflow_id")
    parser.add_argument("--checkpoint-hash", required=True)


def register_phase11_shadow_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    phase11 = subparsers.add_parser(
        "phase11-shadow",
        help="Operate the offline Phase 11 workflow through explicit human gates.",
    )
    commands = phase11.add_subparsers(dest="phase11_command", required=True)

    start = commands.add_parser("start", help="Run decision-time agents in shadow mode.")
    start.add_argument("--analysis-run-id", type=int, required=True)
    start.add_argument("--degree-resolution-id", type=int, required=True)
    start.add_argument("--cutoff", required=True)
    start.add_argument("--executed-at", required=True)
    start.add_argument("--blind", action="store_true")
    _add_model_gate(start)
    _add_commit(start)

    approve = commands.add_parser(
        "approve-forecast", help="Human-approve one frozen candidate and typed claims."
    )
    _add_transition(approve)
    approve.add_argument("--proposal-hash", required=True)
    approve.add_argument("--confirm-proposal-hash", required=True)
    approve.add_argument("--selected-candidate-id", required=True)
    approve.add_argument("--human-actor", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.add_argument(
        "--direction", choices=tuple(item.value for item in ForecastDirection), required=True
    )
    approve.add_argument("--claims-file", required=True)
    approve.add_argument("--approval-note", required=True)
    _add_commit(approve)

    observe = commands.add_parser(
        "register-observations", help="Register caller-supplied Phase 11B observations."
    )
    _add_transition(observe)
    observe.add_argument("--observation-file", required=True)
    observe.add_argument("--accepted-at", required=True)
    _add_commit(observe)

    evaluate = commands.add_parser(
        "evaluate", help="Run deterministic Phase 11B outcome evaluation."
    )
    _add_transition(evaluate)
    evaluate.add_argument("--evaluated-at", required=True)
    evaluate.add_argument("--lower-timeframe-observation-file", action="append", default=[])
    _add_commit(evaluate)

    draft_review = commands.add_parser(
        "draft-review", help="Run the advisory Outcome Reviewer Agent."
    )
    _add_transition(draft_review)
    draft_review.add_argument("--forecast-id", required=True)
    draft_review.add_argument("--forecast-hash", required=True)
    draft_review.add_argument("--observation-set-id", required=True)
    draft_review.add_argument("--observation-set-hash", required=True)
    draft_review.add_argument("--evaluation-id", required=True)
    draft_review.add_argument("--evaluation-hash", required=True)
    draft_review.add_argument("--drafted-at", required=True)
    _add_model_gate(draft_review)
    _add_commit(draft_review)

    record_review = commands.add_parser(
        "record-review", help="Record an explicit human Phase 11C outcome review."
    )
    _add_transition(record_review)
    record_review.add_argument("--draft-id", required=True)
    record_review.add_argument("--draft-hash", required=True)
    record_review.add_argument("--evaluation-hash", required=True)
    record_review.add_argument("--reviewed-hypothesis-id", required=True)
    record_review.add_argument("--human-actor", required=True)
    record_review.add_argument("--reviewed-at", required=True)
    record_review.add_argument(
        "--decision", choices=tuple(item.value for item in ReviewDecision), required=True
    )
    record_review.add_argument("--diagnoses-file", required=True)
    record_review.add_argument("--evidence-reference-id", action="append", default=[])
    record_review.add_argument("--notes", required=True)
    record_review.add_argument("--corrected-interpretation")
    record_review.add_argument("--parent-review-id")
    _add_commit(record_review)

    draft_lesson = commands.add_parser(
        "draft-lesson", help="Run the advisory Mistake Memory Proposal Agent."
    )
    _add_transition(draft_lesson)
    draft_lesson.add_argument("--forecast-id", required=True)
    draft_lesson.add_argument("--forecast-hash", required=True)
    draft_lesson.add_argument("--evaluation-id", required=True)
    draft_lesson.add_argument("--evaluation-hash", required=True)
    draft_lesson.add_argument("--review-id", required=True)
    draft_lesson.add_argument("--review-hash", required=True)
    draft_lesson.add_argument("--additional-support-review-id", action="append", default=[])
    draft_lesson.add_argument("--counterexample-review-id", action="append", default=[])
    draft_lesson.add_argument("--existing-lesson-reference-id")
    draft_lesson.add_argument("--proposed-at", required=True)
    _add_model_gate(draft_lesson)
    _add_commit(draft_lesson)

    record_lesson = commands.add_parser(
        "record-lesson", help="Create only a proposed lesson from an exact draft."
    )
    _add_transition(record_lesson)
    record_lesson.add_argument("--draft-id", required=True)
    record_lesson.add_argument("--draft-hash", required=True)
    record_lesson.add_argument("--confirm-draft-hash", required=True)
    record_lesson.add_argument("--human-actor", required=True)
    record_lesson.add_argument("--recorded-at", required=True)
    _add_commit(record_lesson)

    activate = commands.add_parser(
        "activate-lesson", help="Human-activate an existing proposed lesson."
    )
    _add_transition(activate)
    activate.add_argument("--lesson-id", required=True)
    activate.add_argument("--lesson-hash", required=True)
    activate.add_argument("--confirm-lesson-id", required=True)
    activate.add_argument(
        "--scope-type", choices=tuple(item.value for item in LessonScopeType), required=True
    )
    activate.add_argument("--human-actor", required=True)
    activate.add_argument("--reason", required=True)
    activate.add_argument("--recorded-at", required=True)
    activate.add_argument("--human-exception-reason")
    _add_commit(activate)

    status = commands.add_parser("status", help="Show immutable manual workflow status.")
    status.add_argument("workflow_id")

    verify = commands.add_parser(
        "verify", help="Verify hashes, lineage, cutoffs, references, and SQLite integrity."
    )
    verify.add_argument("workflow_id")


def _require_identity(
    actual_id: str | None,
    actual_hash: str | None,
    expected_id: str,
    expected_hash: str,
    *,
    label: str,
) -> None:
    if actual_id != expected_id or actual_hash != _require_hash(
        expected_hash, field_name=f"{label}_hash"
    ):
        raise ValueError(f"Exact {label} ID/hash confirmation failed.")


def execute_phase11_shadow_command(
    args: argparse.Namespace,
    *,
    settings: Settings,
    store: KnowledgeStore,
    provider_factory: ProviderFactory = create_provider,
    workflow_factory: WorkflowFactory | None = None,
) -> dict[str, Any]:
    """Execute one explicitly selected manual Phase 11 command."""

    command = _require_text(args.phase11_command, field_name="phase11 command")
    checkpoints = Phase11CheckpointStore(_checkpoint_root(settings))

    if command == "status":
        chain = checkpoints.list(args.workflow_id)
        if not chain:
            raise KeyError(f"No durable Phase 11 workflow exists for {args.workflow_id}.")
        return {
            "command": command,
            **_build_status(store, chain),
            "provider_called": False,
            "database_writes": False,
            "checkpoint_writes": False,
        }
    if command == "verify":
        return {"command": command, **verify_phase11_shadow_workflow(store, checkpoints, args.workflow_id)}

    if command == "start":
        if not args.shadow:
            raise ValueError("start requires explicit --shadow.")
        run = store.get_run(args.analysis_run_id)
        resolution = store.get_degree_resolution(args.degree_resolution_id)
        if run is None or resolution is None:
            raise KeyError("The exact analysis run or degree resolution does not exist.")
        if resolution.get("run_id") != args.analysis_run_id:
            raise ValueError("Degree resolution does not belong to the supplied analysis run.")
        request = build_technical_agent_request_from_stored_records(
            run,
            resolution,
            provider=args.provider,
            model=args.model,
            created_at_utc=args.executed_at,
            analysis_cutoff_utc=args.cutoff,
            blind_mode=args.blind,
            shadow_mode=True,
        )
        workflow_id = "shadow_workflow_" + canonical_sha256(
            {"request": request.content_hash}
        )[:32]
        inputs = _operation_inputs(
            request_id=request.request_id,
            request_hash=request.content_hash,
            executed_at_utc=_normalize_utc(args.executed_at, field_name="executed_at"),
        )
        previous, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="start",
            predecessor_hash=None,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        if not args.allow_model_call:
            return _plan(
                command,
                inputs={"workflow_id": workflow_id, **dict(inputs)},
                model_call_required=True,
                allow_model_call=False,
                commit=args.commit,
            )
        provider = provider_factory(
            _explicit_provider_settings(
                settings, provider=args.provider, model=args.model
            )
        )
        workflow = _workflow(store, provider, workflow_factory)
        result = workflow.start_decision_time_analysis(
            request,
            executed_at_utc=args.executed_at,
            shadow_mode=True,
            persist=args.commit,
        )
        if not args.commit:
            return _checkpoint_response(
                command, result, checkpoint=None, inserted=False, provider_called=True
            )
        checkpoint = _new_checkpoint(
            previous=previous,
            operation="start",
            operation_key=operation_key,
            result=result,
            recorded_at_utc=args.executed_at,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return _checkpoint_response(
            command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=True
        )

    workflow_id = _safe_component(args.workflow_id, field_name="workflow_id")
    predecessor_hash = _require_hash(
        args.checkpoint_hash, field_name="checkpoint_hash"
    )

    if command == "approve-forecast":
        if args.proposal_hash != args.confirm_proposal_hash:
            raise ValueError("--confirm-proposal-hash must exactly repeat --proposal-hash.")
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.DECISION_TIME_ANALYSIS.value
        )
        proposal = previous_result.proposal
        if proposal is None or proposal.content_hash != _require_hash(
            args.proposal_hash, field_name="proposal_hash"
        ):
            raise ValueError("The confirmed proposal hash differs from the workflow proposal.")
        targets, invalidations, confirmations, windows = _claims(args.claims_file)
        approval = OperatorApproval.create(
            approval_id="",
            human_actor=args.human_actor,
            approved_at_utc=args.approved_at,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.content_hash,
            selected_candidate_id=args.selected_candidate_id,
            direction=args.direction,
            target_claims=targets,
            invalidation_claims=invalidations,
            confirmation_claims=confirmations,
            expected_completion_windows=windows,
            approval_note=args.approval_note,
        )
        inputs = _operation_inputs(operator_approval_hash=approval.content_hash)
        _, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="approve_forecast",
            predecessor_hash=predecessor_hash,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        workflow = _workflow(store, _NoModelProvider(), workflow_factory)
        result = workflow.approve_forecast(
            previous_result, approval, persist=args.commit
        )
        if not args.commit:
            return _checkpoint_response(command, result, checkpoint=None, inserted=False, provider_called=False)
        checkpoint = _new_checkpoint(
            previous=predecessor,
            operation="approve_forecast",
            operation_key=operation_key,
            result=result,
            recorded_at_utc=args.approved_at,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return _checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=False)

    if command == "register-observations":
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.AWAITING_OBSERVATIONS.value
        )
        observation = ForecastObservationSet.from_dict(
            _load_json(args.observation_file, expected="mapping")
        )
        inputs = _operation_inputs(
            observation_set_id=observation.observation_set_id,
            observation_set_hash=observation.content_hash,
            accepted_at_utc=_normalize_utc(args.accepted_at, field_name="accepted_at"),
        )
        _, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="register_observations",
            predecessor_hash=predecessor_hash,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        workflow = _workflow(store, _NoModelProvider(), workflow_factory)
        result = workflow.accept_observations(
            previous_result,
            observation,
            accepted_at_utc=args.accepted_at,
            persist=args.commit,
        )
        if not args.commit:
            return _checkpoint_response(command, result, checkpoint=None, inserted=False, provider_called=False)
        checkpoint = _new_checkpoint(
            previous=predecessor,
            operation="register_observations",
            operation_key=operation_key,
            result=result,
            recorded_at_utc=args.accepted_at,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return _checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=False)

    if command == "evaluate":
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.OBSERVATIONS_ACCEPTED.value
        )
        lower = tuple(
            ForecastObservationSet.from_dict(_load_json(path, expected="mapping"))
            for path in args.lower_timeframe_observation_file
        )
        inputs = _operation_inputs(
            observation_set_hash=previous_result.observation_set_hash,
            lower_timeframe_hashes=[item.content_hash for item in lower],
            evaluated_at_utc=_normalize_utc(args.evaluated_at, field_name="evaluated_at"),
        )
        _, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="evaluate",
            predecessor_hash=predecessor_hash,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        workflow = _workflow(store, _NoModelProvider(), workflow_factory)
        result = workflow.evaluate(
            previous_result,
            evaluated_at_utc=args.evaluated_at,
            lower_timeframe_observation_sets=lower,
            persist=args.commit,
        )
        if not args.commit:
            return _checkpoint_response(command, result, checkpoint=None, inserted=False, provider_called=False)
        checkpoint = _new_checkpoint(
            previous=predecessor,
            operation="evaluate",
            operation_key=operation_key,
            result=result,
            recorded_at_utc=args.evaluated_at,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return _checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=False)

    if command == "draft-review":
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.DETERMINISTIC_EVALUATION.value
        )
        _require_identity(previous_result.forecast_id, previous_result.forecast_hash, args.forecast_id, args.forecast_hash, label="forecast")
        _require_identity(previous_result.observation_set_id, previous_result.observation_set_hash, args.observation_set_id, args.observation_set_hash, label="observation_set")
        _require_identity(previous_result.evaluation_id, previous_result.evaluation_hash, args.evaluation_id, args.evaluation_hash, label="evaluation")
        inputs = _operation_inputs(
            forecast_hash=args.forecast_hash,
            observation_set_hash=args.observation_set_hash,
            evaluation_hash=args.evaluation_hash,
            provider=args.provider,
            model=args.model,
            drafted_at_utc=_normalize_utc(args.drafted_at, field_name="drafted_at"),
        )
        _, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="draft_review",
            predecessor_hash=predecessor_hash,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        if not args.allow_model_call:
            return _plan(command, inputs=inputs, model_call_required=True, allow_model_call=False, commit=args.commit)
        provider = provider_factory(
            _explicit_provider_settings(
                settings, provider=args.provider, model=args.model
            )
        )
        workflow = _workflow(store, provider, workflow_factory)
        result = workflow.draft_outcome_review(
            previous_result,
            drafted_at_utc=args.drafted_at,
            shadow_mode=True,
            persist=args.commit,
        )
        if not args.commit:
            return _checkpoint_response(command, result, checkpoint=None, inserted=False, provider_called=True)
        checkpoint = _new_checkpoint(previous=predecessor, operation="draft_review", operation_key=operation_key, result=result, recorded_at_utc=args.drafted_at)
        stored, inserted = checkpoints.commit(checkpoint)
        return _checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=True)

    if command == "record-review":
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT.value
        )
        draft = previous_result.outcome_review_draft
        if draft is None:
            raise ValueError("The workflow contains no outcome-review draft.")
        if draft.draft_id != args.draft_id or draft.content_hash != _require_hash(args.draft_hash, field_name="draft_hash"):
            raise ValueError("Exact outcome-review draft ID/hash confirmation failed.")
        if previous_result.evaluation_hash != _require_hash(args.evaluation_hash, field_name="evaluation_hash"):
            raise ValueError("Exact deterministic evaluation hash confirmation failed.")
        if args.reviewed_hypothesis_id != draft.reviewed_hypothesis_id:
            raise ValueError("Reviewed hypothesis differs from the frozen advisory draft.")
        evaluation = store.get_forecast_outcome_evaluation(previous_result.evaluation_id)
        if evaluation is None:
            raise KeyError("The deterministic evaluation is missing.")
        parent = store.get_forecast_outcome_review(args.parent_review_id) if args.parent_review_id else None
        diagnoses = _diagnoses(args.diagnoses_file)
        if not args.evidence_reference_id:
            raise ValueError("record-review requires explicit --evidence-reference-id values.")
        review = create_forecast_outcome_review(
            evaluation,
            reviewer_reference=args.human_actor,
            reviewed_at_utc=args.reviewed_at,
            decision=args.decision,
            notes=args.notes,
            reviewed_hypothesis_id=args.reviewed_hypothesis_id,
            diagnoses=diagnoses,
            corrected_interpretation=args.corrected_interpretation,
            evidence_reference_ids=tuple(args.evidence_reference_id),
            parent_review=parent,
        )
        inputs = _operation_inputs(review_id=review.review_id, review_hash=review.content_hash, draft_hash=draft.content_hash)
        _, repeated, operation_key = _prepare_transition(checkpoints, workflow_id=workflow_id, operation="record_review", predecessor_hash=predecessor_hash, inputs=inputs)
        if repeated is not None:
            return _repeated_response(command, repeated)
        workflow = _workflow(store, _NoModelProvider(), workflow_factory)
        continuing = review.decision in {ReviewDecision.APPROVED, ReviewDecision.REVISED}
        if continuing:
            result = workflow.accept_human_outcome_review(
                previous_result,
                review,
                require_persisted_review=False,
            )
        else:
            result = previous_result
        if not args.commit:
            return {
                **_checkpoint_response(command, result, checkpoint=None, inserted=False, provider_called=False),
                "review": review.to_dict(),
            }
        store.create_forecast_outcome_review(review)
        if continuing:
            result = workflow.accept_human_outcome_review(previous_result, review)
            stage = result.current_stage.value
            kind = ManualCheckpointKind.WORKFLOW_RESULT
        else:
            stage = "human_outcome_review_recorded"
            kind = ManualCheckpointKind.HUMAN_ACTION
        checkpoint = _new_checkpoint(
            previous=predecessor,
            operation="record_review",
            operation_key=operation_key,
            result=result,
            recorded_at_utc=args.reviewed_at,
            stage=stage,
            checkpoint_kind=kind,
            action_ids={"human_review": review.review_id},
            action_hashes={"human_review": review.content_hash},
            human_action_required=bool(result.human_gate) if continuing else False,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return {
            **_checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=False),
            "review": review.to_dict(),
        }

    if command == "draft-lesson":
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW.value
        )
        _require_identity(previous_result.forecast_id, previous_result.forecast_hash, args.forecast_id, args.forecast_hash, label="forecast")
        _require_identity(previous_result.evaluation_id, previous_result.evaluation_hash, args.evaluation_id, args.evaluation_hash, label="evaluation")
        _require_identity(previous_result.human_review_id, previous_result.human_review_hash, args.review_id, args.review_hash, label="review")
        supporting = tuple(_review_bundle(store, item) for item in args.additional_support_review_id)
        counterexamples = tuple(_review_bundle(store, item) for item in args.counterexample_review_id)
        inputs = _operation_inputs(
            review_hash=args.review_hash,
            supporting_review_hashes=[item[2].content_hash for item in supporting],
            counterexample_review_hashes=[item[2].content_hash for item in counterexamples],
            existing_lesson_reference_id=args.existing_lesson_reference_id,
            provider=args.provider,
            model=args.model,
            proposed_at_utc=_normalize_utc(args.proposed_at, field_name="proposed_at"),
        )
        _, repeated, operation_key = _prepare_transition(checkpoints, workflow_id=workflow_id, operation="draft_lesson", predecessor_hash=predecessor_hash, inputs=inputs)
        if repeated is not None:
            return _repeated_response(command, repeated)
        if not args.allow_model_call:
            return _plan(command, inputs=inputs, model_call_required=True, allow_model_call=False, commit=args.commit)
        provider = provider_factory(
            _explicit_provider_settings(
                settings, provider=args.provider, model=args.model
            )
        )
        workflow = _workflow(store, provider, workflow_factory)
        result = workflow.propose_mistake_memory(
            previous_result,
            proposed_at_utc=args.proposed_at,
            shadow_mode=True,
            additional_supporting_bundles=supporting,
            counterexample_bundles=counterexamples,
            existing_lesson_reference_id=args.existing_lesson_reference_id,
            persist=args.commit,
        )
        if not args.commit:
            return _checkpoint_response(command, result, checkpoint=None, inserted=False, provider_called=True)
        checkpoint = _new_checkpoint(previous=predecessor, operation="draft_lesson", operation_key=operation_key, result=result, recorded_at_utc=args.proposed_at)
        stored, inserted = checkpoints.commit(checkpoint)
        return _checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=True)

    if command == "record-lesson":
        if args.draft_hash != args.confirm_draft_hash:
            raise ValueError("--confirm-draft-hash must exactly repeat --draft-hash.")
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        previous_result = _exact_previous(
            predecessor, expected_stage=ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL.value
        )
        draft = previous_result.mistake_lesson_proposal_draft
        if draft is None or draft.draft_id != args.draft_id or draft.content_hash != _require_hash(args.draft_hash, field_name="draft_hash"):
            raise ValueError("Exact mistake-lesson draft ID/hash confirmation failed.")
        recorded_at_utc = _normalize_utc(args.recorded_at, field_name="recorded_at")
        inputs = _operation_inputs(
            draft_hash=draft.content_hash,
            human_actor=args.human_actor,
            recorded_at_utc=recorded_at_utc,
        )
        _, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="record_lesson",
            predecessor_hash=predecessor_hash,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        supporting_bundles = tuple(_review_bundle(store, item) for item in draft.supporting_review_ids)
        counter_bundles = tuple(_review_bundle(store, item) for item in draft.counterexample_review_ids)
        all_bundles = supporting_bundles + counter_bundles
        evaluations = {item[1].evaluation_id: item[1] for item in all_bundles}
        forecast_symbols = {item[0].forecast_id: item[0].symbol for item in all_bundles}
        lesson, sources, proposed_event = propose_mistake_memory_lesson(
            tuple(item[2] for item in supporting_bundles),
            evaluations,
            forecast_symbols=forecast_symbols,
            scope=draft.recommended_scope,
            title=draft.title,
            warning_text=draft.warning_text,
            recommended_audit_check=draft.recommended_audit_check,
            proposed_by=args.human_actor,
            proposed_at_utc=args.recorded_at,
            counterexample_reviews=tuple(item[2] for item in counter_bundles),
            existing_lessons=_active_lessons_at(store, draft.generated_at_utc),
        )
        if set(lesson.possible_duplicate_lesson_ids) != set(draft.possible_duplicate_lesson_ids):
            raise ValueError("Current deterministic duplicate set differs from the frozen lesson draft.")
        if not args.commit:
            return {
                "command": command,
                "status": "dry_run_completed",
                "dry_run": True,
                "provider_called": False,
                "database_writes": False,
                "checkpoint_writes": False,
                "human_action_required": True,
                "lesson": lesson.to_dict(),
                "sources": [item.to_dict() for item in sources],
                "proposal_event": proposed_event.to_dict(),
            }
        store.create_mistake_memory_lesson(lesson)
        for source in sources:
            store.create_mistake_memory_source(source)
        store.create_mistake_memory_event(proposed_event)
        action_ids = {"lesson": lesson.lesson_id, "proposal_event": proposed_event.event_id}
        action_hashes = {"lesson": lesson.content_hash, "proposal_event": proposed_event.content_hash}
        for source in sources:
            key = f"lesson_source:{source.source_id}"
            action_ids[key] = source.source_id
            action_hashes[key] = source.content_hash
        checkpoint = _new_checkpoint(
            previous=predecessor,
            operation="record_lesson",
            operation_key=operation_key,
            result=previous_result,
            recorded_at_utc=args.recorded_at,
            stage="lesson_recorded",
            checkpoint_kind=ManualCheckpointKind.HUMAN_ACTION,
            action_ids=action_ids,
            action_hashes=action_hashes,
            human_action_required=True,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return {
            **_checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=False),
            "lesson": lesson.to_dict(),
            "lesson_status": LessonStatus.PROPOSED.value,
        }

    if command == "activate-lesson":
        if args.lesson_id != args.confirm_lesson_id:
            raise ValueError("--confirm-lesson-id must exactly repeat --lesson-id.")
        predecessor = checkpoints.by_hash(workflow_id, predecessor_hash)
        if predecessor.stage != "lesson_recorded":
            raise ValueError("activate-lesson requires the immutable lesson_recorded stage.")
        previous_result = replay_shadow_workflow_result(predecessor.workflow_result)
        lesson = store.get_mistake_memory_lesson(args.lesson_id)
        if lesson is None or lesson.content_hash != _require_hash(args.lesson_hash, field_name="lesson_hash"):
            raise ValueError("Exact lesson ID/hash confirmation failed.")
        if lesson.scope.scope_type.value != args.scope_type:
            raise ValueError("Explicit scope classification differs from the proposed lesson.")
        recorded_at_utc = _normalize_utc(args.recorded_at, field_name="recorded_at")
        inputs = _operation_inputs(
            lesson_hash=lesson.content_hash,
            scope_type=args.scope_type,
            human_actor=args.human_actor,
            reason=args.reason,
            recorded_at_utc=recorded_at_utc,
            human_exception_reason=args.human_exception_reason,
        )
        _, repeated, operation_key = _prepare_transition(
            checkpoints,
            workflow_id=workflow_id,
            operation="activate_lesson",
            predecessor_hash=predecessor_hash,
            inputs=inputs,
        )
        if repeated is not None:
            return _repeated_response(command, repeated)
        sources = tuple(store.list_mistake_memory_sources(lesson_id=lesson.lesson_id, limit=10_000))
        events = tuple(store.list_mistake_memory_events(lesson_id=lesson.lesson_id, limit=10_000))
        active_event = approve_lesson(
            lesson,
            events,
            sources,
            actor_reference=args.human_actor,
            recorded_at_utc=args.recorded_at,
            reason=args.reason,
            human_exception_reason=args.human_exception_reason,
        )
        if not args.commit:
            return {
                "command": command,
                "status": "dry_run_completed",
                "dry_run": True,
                "provider_called": False,
                "database_writes": False,
                "checkpoint_writes": False,
                "human_action_required": True,
                "activation_event": active_event.to_dict(),
            }
        store.create_mistake_memory_event(active_event)
        checkpoint = _new_checkpoint(
            previous=predecessor,
            operation="activate_lesson",
            operation_key=operation_key,
            result=previous_result,
            recorded_at_utc=args.recorded_at,
            stage="lesson_activated",
            checkpoint_kind=ManualCheckpointKind.HUMAN_ACTION,
            action_ids={"lesson": lesson.lesson_id, "activation_event": active_event.event_id},
            action_hashes={"lesson": lesson.content_hash, "activation_event": active_event.content_hash},
            human_action_required=False,
        )
        stored, inserted = checkpoints.commit(checkpoint)
        return {
            **_checkpoint_response(command, stored.workflow_result, checkpoint=stored, inserted=inserted, provider_called=False),
            "lesson": lesson.to_dict(),
            "lesson_status": LessonStatus.ACTIVE.value,
        }

    raise ValueError(f"Unknown phase11-shadow command: {command}.")


__all__ = [
    "MANUAL_CHECKPOINT_SCHEMA_VERSION",
    "PHASE11_CLI_POLICY_VERSION",
    "PHASE11_SHADOW_STATE_DIRECTORY",
    "ManualCheckpointKind",
    "ManualShadowWorkflowCheckpoint",
    "Phase11CheckpointStore",
    "execute_phase11_shadow_command",
    "register_phase11_shadow_commands",
    "verify_phase11_shadow_workflow",
]
