"""Pure Phase 11E1 six-agent shadow workflow.

The module composes the existing Phase 11 ledgers and agent orchestrators. It
does not fetch market data, create human reviews or lessons, or participate in
the active analysis pipeline. Workflow checkpoints are immutable return values;
durable source artifacts continue to use their existing append-only ledgers.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .correction_semantics import structure_family
from .forecast_outcomes import (
    ForecastObservationSet,
    ForecastOutcomeEvaluation,
    OutcomeStatus,
    evaluate_forecast_observation_set,
    forecast_observation_set_content_hash,
    forecast_outcome_evaluation_content_hash,
    validate_forecast_observation_set,
    validate_forecast_outcome_evaluation,
)
from .forecast_records import (
    AlternativeHypothesis,
    ConfirmationClaim,
    DatasetCutoff,
    EvaluationEligibility,
    EvaluationEligibilityStatus,
    ExpectedCompletionWindow,
    EvidenceRuleClass,
    ForecastDirection,
    ForecastEvidence,
    ForecastEvidenceKind,
    ForecastEvidenceStatus,
    ForecastRecord,
    ForecastRecordState,
    InvalidationClaim,
    MainHypothesis,
    TargetClaim,
    canonical_sha256,
    dataset_cutoff_from_stored_summary,
    forecast_record_content_hash,
    stored_record_content_hash,
    validate_forecast_record,
)
from .market_scenario import FrozenTechnicalPremise, ScenarioDirection
from .mistake_memory import (
    ForecastOutcomeReview,
    LessonEvent,
    LessonSource,
    LessonStatus,
    MistakeMemoryLesson,
    ReviewDecision,
    lesson_current_status,
    validate_forecast_outcome_review,
    validate_lesson_event_chain,
)
from .outcome_learning_agents import (
    MistakeLessonProposalDraft,
    OutcomeLearningAgentOrchestrator,
    OutcomeLearningOrchestrationStatus,
    OutcomeReviewDraft,
    build_mistake_memory_proposal_request,
    build_outcome_reviewer_request,
)
from .technical_agent_orchestrator import (
    CandidateValidationResult,
    CandidateWaveCount,
    EvidenceAuditResult,
    ShadowResolutionStatus,
    TechnicalAgentOrchestration,
    TechnicalAgentOrchestrator,
    TechnicalAgentRequest,
    TechnicalAgentRole,
    technical_counter_input_hash,
    validate_technical_agent_orchestration,
)


SHADOW_WORKFLOW_SCHEMA_VERSION = "phase11-shadow-workflow-result-1.0.0"
SHADOW_WORKFLOW_POLICY_VERSION = "phase11e1-shadow-policy-1.0.0"
SHADOW_WORKFLOW_CALCULATION_VERSION = "phase11e1-deterministic-1.0.0"
SHADOW_WORKFLOW_CHECKPOINT_SCHEMA_VERSION = "phase11-shadow-checkpoint-1.0.0"
SHADOW_FORECAST_PROPOSAL_SCHEMA_VERSION = "phase11-shadow-proposal-1.0.0"
OPERATOR_APPROVAL_SCHEMA_VERSION = "phase11-operator-approval-1.0.0"
HUMAN_ACTION_GATE_SCHEMA_VERSION = "phase11-human-action-gate-1.0.0"
SHADOW_WORKFLOW_LINEAGE_SCHEMA_VERSION = "phase11-shadow-lineage-1.0.0"


class ShadowWorkflowStage(StrEnum):
    DECISION_TIME_ANALYSIS = "decision_time_analysis"
    HUMAN_FORECAST_APPROVAL = "human_forecast_approval"
    AWAITING_OBSERVATIONS = "awaiting_observations"
    OBSERVATIONS_ACCEPTED = "observations_accepted"
    DETERMINISTIC_EVALUATION = "deterministic_evaluation"
    OUTCOME_REVIEW_DRAFT = "outcome_review_draft"
    HUMAN_OUTCOME_REVIEW = "human_outcome_review"
    MISTAKE_MEMORY_PROPOSAL = "mistake_memory_proposal"
    FUTURE_ANALYSIS_PROOF = "future_analysis_proof"
    COMPLETED = "completed"


class ShadowWorkflowStatus(StrEnum):
    READY = "ready"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    AWAITING_OBSERVATIONS = "awaiting_observations"
    COMPLETED = "completed"
    STOPPED_NO_LEARNING = "stopped_no_learning"
    FAILED = "failed"


_ALLOWED_TRANSITIONS = frozenset(
    {
        (ShadowWorkflowStage.DECISION_TIME_ANALYSIS, ShadowWorkflowStage.HUMAN_FORECAST_APPROVAL),
        (ShadowWorkflowStage.HUMAN_FORECAST_APPROVAL, ShadowWorkflowStage.AWAITING_OBSERVATIONS),
        (ShadowWorkflowStage.AWAITING_OBSERVATIONS, ShadowWorkflowStage.OBSERVATIONS_ACCEPTED),
        (ShadowWorkflowStage.OBSERVATIONS_ACCEPTED, ShadowWorkflowStage.DETERMINISTIC_EVALUATION),
        (ShadowWorkflowStage.DETERMINISTIC_EVALUATION, ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT),
        (ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT, ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW),
        (ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW, ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL),
        (ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW, ShadowWorkflowStage.COMPLETED),
        (ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL, ShadowWorkflowStage.FUTURE_ANALYSIS_PROOF),
        (ShadowWorkflowStage.FUTURE_ANALYSIS_PROOF, ShadowWorkflowStage.COMPLETED),
    }
)


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
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported shadow-workflow JSON value: {type(value).__name__}.")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys.")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StrEnum) or value is None or isinstance(
        value, (str, int, float, bool)
    ):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _freeze_json(to_dict())
    raise TypeError(f"Unsupported immutable value: {type(value).__name__}.")


def _contract_hash(value: Any) -> str:
    payload = value.to_dict() if hasattr(value, "to_dict") else dict(value)
    payload = dict(payload)
    payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _normalize_utc(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name=field_name)


def _require_hash(value: Any, *, field_name: str) -> str:
    text = _require_text(value, field_name=field_name)
    if len(text) != 64 or any(item not in "0123456789abcdef" for item in text):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return text


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} has an unsupported value.") from exc


def _string_tuple(value: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} cannot contain duplicates.")
    return result


def _string_mapping(
    value: Mapping[str, str], *, field_name: str, hashes: bool = False
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a string mapping.")
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = _require_text(key, field_name=f"{field_name}.key")
        result[normalized_key] = (
            _require_hash(item, field_name=f"{field_name}[{key!r}]")
            if hashes
            else _require_text(item, field_name=f"{field_name}[{key!r}]")
        )
    return MappingProxyType(dict(sorted(result.items())))


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, model_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


def _models(
    value: Sequence[Any], model_type: type[Any], *, field_name: str
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result = []
    for item in value:
        if isinstance(item, model_type):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(model_type.from_dict(item))
        else:
            raise TypeError(f"{field_name} contains an invalid item.")
    return tuple(result)


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class ShadowWorkflowCheckpoint(_JsonContract):
    checkpoint_id: str
    workflow_id: str
    sequence_number: int
    stage: ShadowWorkflowStage
    previous_checkpoint_id: str | None
    previous_checkpoint_hash: str | None
    input_hashes: Mapping[str, str]
    output_ids: Mapping[str, str]
    output_hashes: Mapping[str, str]
    human_action_required: bool
    completed_at_utc: str
    stage_version: str = SHADOW_WORKFLOW_POLICY_VERSION
    schema_version: str = SHADOW_WORKFLOW_CHECKPOINT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "checkpoint_id", _require_text(self.checkpoint_id, field_name="checkpoint_id"))
        object.__setattr__(self, "workflow_id", _require_text(self.workflow_id, field_name="workflow_id"))
        if not isinstance(self.sequence_number, int) or isinstance(self.sequence_number, bool) or self.sequence_number < 1:
            raise ValueError("sequence_number must be a positive integer.")
        object.__setattr__(self, "stage", _enum_value(self.stage, ShadowWorkflowStage, field_name="stage"))
        object.__setattr__(self, "previous_checkpoint_id", _optional_text(self.previous_checkpoint_id, field_name="previous_checkpoint_id"))
        object.__setattr__(self, "previous_checkpoint_hash", None if self.previous_checkpoint_hash is None else _require_hash(self.previous_checkpoint_hash, field_name="previous_checkpoint_hash"))
        if (self.previous_checkpoint_id is None) != (self.previous_checkpoint_hash is None):
            raise ValueError("Previous checkpoint ID and hash must be supplied together.")
        if self.sequence_number == 1 and self.previous_checkpoint_id is not None:
            raise ValueError("The first checkpoint cannot name a predecessor.")
        if self.sequence_number > 1 and self.previous_checkpoint_id is None:
            raise ValueError("Later checkpoints require a predecessor.")
        object.__setattr__(self, "input_hashes", _string_mapping(self.input_hashes, field_name="input_hashes", hashes=True))
        object.__setattr__(self, "output_ids", _string_mapping(self.output_ids, field_name="output_ids"))
        object.__setattr__(self, "output_hashes", _string_mapping(self.output_hashes, field_name="output_hashes", hashes=True))
        if set(self.output_ids) != set(self.output_hashes):
            raise ValueError("Checkpoint output IDs and hashes must use identical keys.")
        if not isinstance(self.human_action_required, bool):
            raise TypeError("human_action_required must be boolean.")
        object.__setattr__(self, "completed_at_utc", _normalize_utc(self.completed_at_utc, field_name="completed_at_utc"))
        object.__setattr__(self, "stage_version", _require_text(self.stage_version, field_name="stage_version"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ShadowWorkflowCheckpoint":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("checkpoint_id"):
            values["checkpoint_id"] = "shadow_checkpoint_" + canonical_sha256(
                {
                    "workflow_id": values.get("workflow_id"),
                    "sequence_number": values.get("sequence_number"),
                    "stage": _json_value(values.get("stage")),
                    "previous_checkpoint_hash": values.get("previous_checkpoint_hash"),
                    "input_hashes": values.get("input_hashes", {}),
                    "output_hashes": values.get("output_hashes", {}),
                    "completed_at_utc": values.get("completed_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_contract_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ShadowWorkflowCheckpoint":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _contract_hash(result):
            raise ValueError("ShadowWorkflowCheckpoint content_hash does not match.")
        return result


@dataclass(frozen=True, slots=True)
class ShadowForecastProposal(_JsonContract):
    proposal_id: str
    workflow_id: str
    technical_orchestration_id: str
    technical_orchestration_hash: str
    technical_request_id: str
    technical_request_hash: str
    source_analysis_run_id: int
    source_analysis_run_hash: str
    source_degree_resolution_id: int
    source_degree_resolution_hash: str
    analysis_cutoff_utc: str
    candidate_ids: tuple[str, ...]
    candidate_hashes: Mapping[str, str]
    validation_ids: Mapping[str, str]
    validation_hashes: Mapping[str, str]
    audit_ids: Mapping[str, str]
    audit_hashes: Mapping[str, str]
    eligible_candidate_ids: tuple[str, ...]
    ineligible_candidate_ids: tuple[str, ...]
    proposed_selected_candidate_id: str | None
    retrieved_lesson_ids: tuple[str, ...]
    retrieved_lesson_hashes: Mapping[str, str]
    candidate_freeze_hash: str
    hard_validation_hash: str
    stage_trace: tuple[str, ...]
    human_action_required: bool
    created_at_utc: str
    schema_version: str = SHADOW_FORECAST_PROPOSAL_SCHEMA_VERSION
    policy_version: str = SHADOW_WORKFLOW_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("proposal_id", "workflow_id", "technical_orchestration_id", "technical_request_id", "schema_version", "policy_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in ("technical_orchestration_hash", "technical_request_hash", "source_analysis_run_hash", "source_degree_resolution_hash", "candidate_freeze_hash", "hard_validation_hash"):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        for name in ("source_analysis_run_id", "source_degree_resolution_id"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer.")
        object.__setattr__(self, "analysis_cutoff_utc", _normalize_utc(self.analysis_cutoff_utc, field_name="analysis_cutoff_utc"))
        for name in ("candidate_ids", "eligible_candidate_ids", "ineligible_candidate_ids", "retrieved_lesson_ids", "stage_trace"):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name))
        for name in ("candidate_hashes", "validation_hashes", "audit_hashes", "retrieved_lesson_hashes"):
            object.__setattr__(self, name, _string_mapping(getattr(self, name), field_name=name, hashes=True))
        for name in ("validation_ids", "audit_ids"):
            object.__setattr__(self, name, _string_mapping(getattr(self, name), field_name=name))
        if set(self.candidate_ids) != set(self.candidate_hashes):
            raise ValueError("Candidate IDs and hashes differ.")
        if set(self.validation_ids) != set(self.validation_hashes):
            raise ValueError("Validation IDs and hashes differ.")
        if set(self.audit_ids) != set(self.audit_hashes):
            raise ValueError("Audit IDs and hashes differ.")
        if set(self.eligible_candidate_ids) & set(self.ineligible_candidate_ids):
            raise ValueError("A candidate cannot be both eligible and ineligible.")
        if set(self.eligible_candidate_ids) | set(self.ineligible_candidate_ids) != set(self.candidate_ids):
            raise ValueError("Proposal eligibility must classify every candidate.")
        object.__setattr__(self, "proposed_selected_candidate_id", _optional_text(self.proposed_selected_candidate_id, field_name="proposed_selected_candidate_id"))
        if self.proposed_selected_candidate_id is not None and self.proposed_selected_candidate_id not in self.eligible_candidate_ids:
            raise ValueError("The proposed selection must be an eligible candidate.")
        expected_trace = ("primary_frozen", "alternative_frozen", "candidates_validated", "lessons_retrieved")
        positions = []
        for item in expected_trace:
            if item not in self.stage_trace:
                raise ValueError(f"Technical stage trace is missing {item}.")
            positions.append(self.stage_trace.index(item))
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            raise ValueError("Candidate freezing, validation and lesson retrieval are out of order.")
        if not isinstance(self.human_action_required, bool):
            raise TypeError("human_action_required must be boolean.")
        if self.human_action_required != bool(self.eligible_candidate_ids):
            raise ValueError("Proposal human-action state must match candidate availability.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ShadowForecastProposal":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("proposal_id"):
            values["proposal_id"] = "shadow_proposal_" + canonical_sha256(
                {
                    "workflow_id": values.get("workflow_id"),
                    "technical_orchestration_hash": values.get("technical_orchestration_hash"),
                    "created_at_utc": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_contract_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ShadowForecastProposal":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _contract_hash(result):
            raise ValueError("ShadowForecastProposal content_hash does not match.")
        return result


@dataclass(frozen=True, slots=True)
class OperatorApproval(_JsonContract):
    approval_id: str
    human_actor: str
    approved_at_utc: str
    proposal_id: str
    proposal_hash: str
    selected_candidate_id: str
    direction: ForecastDirection
    target_claims: tuple[TargetClaim, ...]
    invalidation_claims: tuple[InvalidationClaim, ...]
    confirmation_claims: tuple[ConfirmationClaim, ...]
    expected_completion_windows: tuple[ExpectedCompletionWindow, ...]
    approval_note: str
    schema_version: str = OPERATOR_APPROVAL_SCHEMA_VERSION
    policy_version: str = SHADOW_WORKFLOW_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("approval_id", "human_actor", "proposal_id", "selected_candidate_id", "approval_note", "schema_version", "policy_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "approved_at_utc", _normalize_utc(self.approved_at_utc, field_name="approved_at_utc"))
        object.__setattr__(self, "proposal_hash", _require_hash(self.proposal_hash, field_name="proposal_hash"))
        object.__setattr__(self, "direction", _enum_value(self.direction, ForecastDirection, field_name="direction"))
        for name, model in (
            ("target_claims", TargetClaim),
            ("invalidation_claims", InvalidationClaim),
            ("confirmation_claims", ConfirmationClaim),
            ("expected_completion_windows", ExpectedCompletionWindow),
        ):
            object.__setattr__(self, name, _models(getattr(self, name), model, field_name=name))
        if not any(item.hypothesis_id == self.selected_candidate_id for item in self.target_claims):
            raise ValueError("Operator approval requires a typed target for the selected candidate.")
        if not any(item.hypothesis_id == self.selected_candidate_id for item in self.invalidation_claims):
            raise ValueError("Operator approval requires a typed invalidation for the selected candidate.")
        if not any(item.hypothesis_id == self.selected_candidate_id for item in self.expected_completion_windows):
            raise ValueError("Operator approval requires a completion window for the selected candidate.")
        if any(
            item.hypothesis_id == self.selected_candidate_id and item.direction is not self.direction
            for item in self.target_claims
        ):
            raise ValueError("Selected target direction differs from the operator direction.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "OperatorApproval":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("confirmation_claims", ())
        if not values.get("approval_id"):
            values["approval_id"] = "operator_approval_" + canonical_sha256(
                {
                    "human_actor": values.get("human_actor"),
                    "approved_at_utc": values.get("approved_at_utc"),
                    "proposal_hash": values.get("proposal_hash"),
                    "selected_candidate_id": values.get("selected_candidate_id"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_contract_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "OperatorApproval":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _contract_hash(result):
            raise ValueError("OperatorApproval content_hash does not match.")
        return result


@dataclass(frozen=True, slots=True)
class HumanActionGate(_JsonContract):
    gate_id: str
    workflow_id: str
    stage: ShadowWorkflowStage
    required_action: str
    reason: str
    source_ids: Mapping[str, str]
    source_hashes: Mapping[str, str]
    created_at_utc: str
    human_action_required: bool = True
    schema_version: str = HUMAN_ACTION_GATE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("gate_id", "workflow_id", "required_action", "reason", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "stage", _enum_value(self.stage, ShadowWorkflowStage, field_name="stage"))
        object.__setattr__(self, "source_ids", _string_mapping(self.source_ids, field_name="source_ids"))
        object.__setattr__(self, "source_hashes", _string_mapping(self.source_hashes, field_name="source_hashes", hashes=True))
        if set(self.source_ids) != set(self.source_hashes):
            raise ValueError("Human gate source IDs and hashes must use identical keys.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        if self.human_action_required is not True:
            raise ValueError("Every HumanActionGate must require human action.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "HumanActionGate":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("gate_id"):
            values["gate_id"] = "human_gate_" + canonical_sha256(
                {
                    "workflow_id": values.get("workflow_id"),
                    "stage": _json_value(values.get("stage")),
                    "required_action": values.get("required_action"),
                    "source_hashes": values.get("source_hashes", {}),
                    "created_at_utc": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_contract_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "HumanActionGate":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _contract_hash(result):
            raise ValueError("HumanActionGate content_hash does not match.")
        return result


@dataclass(frozen=True, slots=True)
class ShadowWorkflowLineage(_JsonContract):
    workflow_id: str
    root_request_id: str
    root_request_hash: str
    checkpoints: tuple[ShadowWorkflowCheckpoint, ...]
    artifact_ids: Mapping[str, str]
    artifact_hashes: Mapping[str, str]
    created_at_utc: str
    updated_at_utc: str
    schema_version: str = SHADOW_WORKFLOW_LINEAGE_SCHEMA_VERSION
    policy_version: str = SHADOW_WORKFLOW_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("workflow_id", "root_request_id", "schema_version", "policy_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "root_request_hash", _require_hash(self.root_request_hash, field_name="root_request_hash"))
        object.__setattr__(self, "checkpoints", _models(self.checkpoints, ShadowWorkflowCheckpoint, field_name="checkpoints"))
        object.__setattr__(self, "artifact_ids", _string_mapping(self.artifact_ids, field_name="artifact_ids"))
        object.__setattr__(self, "artifact_hashes", _string_mapping(self.artifact_hashes, field_name="artifact_hashes", hashes=True))
        if set(self.artifact_ids) != set(self.artifact_hashes):
            raise ValueError("Lineage artifact IDs and hashes must use identical keys.")
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "updated_at_utc", _normalize_utc(self.updated_at_utc, field_name="updated_at_utc"))
        if _utc(self.updated_at_utc) < _utc(self.created_at_utc):
            raise ValueError("Lineage update cannot predate creation.")
        errors = _lineage_errors(self)
        if errors:
            raise ValueError("Invalid ShadowWorkflowLineage: " + " ".join(errors))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ShadowWorkflowLineage":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=_contract_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ShadowWorkflowLineage":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _contract_hash(result):
            raise ValueError("ShadowWorkflowLineage content_hash does not match.")
        return result


@dataclass(frozen=True, slots=True)
class ShadowWorkflowResult(_JsonContract):
    workflow_id: str
    status: ShadowWorkflowStatus
    current_stage: ShadowWorkflowStage
    lineage: ShadowWorkflowLineage
    proposal: ShadowForecastProposal | None
    operator_approval: OperatorApproval | None
    forecast_id: str | None
    forecast_hash: str | None
    observation_set_id: str | None
    observation_set_hash: str | None
    evaluation_id: str | None
    evaluation_hash: str | None
    outcome_review_draft: OutcomeReviewDraft | None
    human_review_id: str | None
    human_review_hash: str | None
    mistake_lesson_proposal_draft: MistakeLessonProposalDraft | None
    future_technical_orchestration_id: str | None
    future_technical_orchestration_hash: str | None
    human_gate: HumanActionGate | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    created_at_utc: str
    updated_at_utc: str
    versions: Mapping[str, str]
    schema_version: str = SHADOW_WORKFLOW_SCHEMA_VERSION
    calculation_version: str = SHADOW_WORKFLOW_CALCULATION_VERSION
    policy_version: str = SHADOW_WORKFLOW_POLICY_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("workflow_id", "schema_version", "calculation_version", "policy_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "status", _enum_value(self.status, ShadowWorkflowStatus, field_name="status"))
        object.__setattr__(self, "current_stage", _enum_value(self.current_stage, ShadowWorkflowStage, field_name="current_stage"))
        if isinstance(self.lineage, Mapping):
            object.__setattr__(self, "lineage", ShadowWorkflowLineage.from_dict(self.lineage))
        elif not isinstance(self.lineage, ShadowWorkflowLineage):
            raise TypeError("lineage must be a ShadowWorkflowLineage.")
        for name, model in (
            ("proposal", ShadowForecastProposal),
            ("operator_approval", OperatorApproval),
            ("outcome_review_draft", OutcomeReviewDraft),
            ("mistake_lesson_proposal_draft", MistakeLessonProposalDraft),
            ("human_gate", HumanActionGate),
        ):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, model.from_dict(value))
            elif value is not None and not isinstance(value, model):
                raise TypeError(f"{name} has an invalid type.")
        for left, right in (
            ("forecast_id", "forecast_hash"),
            ("observation_set_id", "observation_set_hash"),
            ("evaluation_id", "evaluation_hash"),
            ("human_review_id", "human_review_hash"),
            ("future_technical_orchestration_id", "future_technical_orchestration_hash"),
        ):
            identifier = _optional_text(getattr(self, left), field_name=left)
            digest = None if getattr(self, right) is None else _require_hash(getattr(self, right), field_name=right)
            if (identifier is None) != (digest is None):
                raise ValueError(f"{left} and {right} must be supplied together.")
            object.__setattr__(self, left, identifier)
            object.__setattr__(self, right, digest)
        object.__setattr__(self, "warnings", _string_tuple(self.warnings, field_name="warnings"))
        object.__setattr__(self, "errors", _string_tuple(self.errors, field_name="errors"))
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        object.__setattr__(self, "updated_at_utc", _normalize_utc(self.updated_at_utc, field_name="updated_at_utc"))
        object.__setattr__(self, "versions", _string_mapping(self.versions, field_name="versions"))
        if self.lineage.workflow_id != self.workflow_id:
            raise ValueError("Result and lineage workflow IDs differ.")
        if not self.lineage.checkpoints or self.lineage.checkpoints[-1].stage is not self.current_stage:
            raise ValueError("Result current stage must equal the latest checkpoint stage.")
        if self.status is ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED and self.human_gate is None:
            raise ValueError("human_action_required results require a HumanActionGate.")
        if self.status is ShadowWorkflowStatus.AWAITING_OBSERVATIONS and self.human_gate is None:
            raise ValueError("awaiting_observations results require a HumanActionGate.")
        if self.human_gate is not None and self.human_gate.workflow_id != self.workflow_id:
            raise ValueError("Human gate belongs to a different workflow.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "ShadowWorkflowResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=_contract_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "ShadowWorkflowResult":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _contract_hash(result):
            raise ValueError("ShadowWorkflowResult content_hash does not match.")
        return result


def _lineage_errors(lineage: ShadowWorkflowLineage) -> tuple[str, ...]:
    errors: list[str] = []
    previous: ShadowWorkflowCheckpoint | None = None
    for index, item in enumerate(lineage.checkpoints, start=1):
        if item.content_hash != _contract_hash(item):
            errors.append(f"Checkpoint {item.checkpoint_id} hash does not match.")
        if item.workflow_id != lineage.workflow_id:
            errors.append(f"Checkpoint {item.checkpoint_id} belongs to another workflow.")
        if item.sequence_number != index:
            errors.append("Checkpoint sequence is not contiguous.")
        if previous is None:
            if item.stage is not ShadowWorkflowStage.DECISION_TIME_ANALYSIS:
                errors.append("The workflow must begin with decision-time analysis.")
        else:
            if item.previous_checkpoint_id != previous.checkpoint_id or item.previous_checkpoint_hash != previous.content_hash:
                errors.append(f"Checkpoint {item.checkpoint_id} predecessor linkage differs.")
            if (previous.stage, item.stage) not in _ALLOWED_TRANSITIONS:
                errors.append(f"Illegal workflow transition: {previous.stage.value} -> {item.stage.value}.")
            if _utc(item.completed_at_utc) < _utc(previous.completed_at_utc):
                errors.append("A checkpoint cannot predate its predecessor.")
        previous = item
    return tuple(errors)


def validate_shadow_workflow_result(
    value: ShadowWorkflowResult | Mapping[str, Any],
) -> tuple[str, ...]:
    try:
        result = value if isinstance(value, ShadowWorkflowResult) else ShadowWorkflowResult.from_dict(value, verify_hash=False)
    except (TypeError, ValueError) as exc:
        return (str(exc),)
    errors = list(_lineage_errors(result.lineage))
    if result.lineage.content_hash != _contract_hash(result.lineage):
        errors.append("Shadow workflow lineage hash does not match.")
    if result.proposal is not None and result.proposal.content_hash != _contract_hash(result.proposal):
        errors.append("Shadow forecast proposal hash does not match.")
    if result.operator_approval is not None and result.operator_approval.content_hash != _contract_hash(result.operator_approval):
        errors.append("Operator approval hash does not match.")
    if result.human_gate is not None and result.human_gate.content_hash != _contract_hash(result.human_gate):
        errors.append("Human action gate hash does not match.")
    if result.content_hash != _contract_hash(result):
        errors.append("Shadow workflow result hash does not match.")
    return tuple(errors)


def replay_shadow_workflow_result(
    value: ShadowWorkflowResult | Mapping[str, Any],
    *,
    expected_content_hash: str | None = None,
) -> ShadowWorkflowResult:
    result = value if isinstance(value, ShadowWorkflowResult) else ShadowWorkflowResult.from_dict(value)
    errors = validate_shadow_workflow_result(result)
    if errors:
        raise ValueError("Shadow workflow replay failed: " + " ".join(errors))
    if expected_content_hash is not None and result.content_hash != expected_content_hash:
        raise ValueError("Shadow workflow result differs from the expected hash.")
    return result


def _checkpoint(
    lineage: ShadowWorkflowLineage | None,
    *,
    workflow_id: str,
    root_request_id: str,
    root_request_hash: str,
    stage: ShadowWorkflowStage,
    input_hashes: Mapping[str, str],
    output_ids: Mapping[str, str],
    output_hashes: Mapping[str, str],
    human_action_required: bool,
    completed_at_utc: str,
) -> ShadowWorkflowLineage:
    previous = lineage.checkpoints[-1] if lineage and lineage.checkpoints else None
    item = ShadowWorkflowCheckpoint.create(
        checkpoint_id="",
        workflow_id=workflow_id,
        sequence_number=(previous.sequence_number + 1 if previous else 1),
        stage=stage,
        previous_checkpoint_id=(previous.checkpoint_id if previous else None),
        previous_checkpoint_hash=(previous.content_hash if previous else None),
        input_hashes=input_hashes,
        output_ids=output_ids,
        output_hashes=output_hashes,
        human_action_required=human_action_required,
        completed_at_utc=completed_at_utc,
    )
    artifact_ids = dict(lineage.artifact_ids) if lineage else {}
    artifact_hashes = dict(lineage.artifact_hashes) if lineage else {}
    artifact_ids.update(output_ids)
    artifact_hashes.update(output_hashes)
    return ShadowWorkflowLineage.create(
        workflow_id=workflow_id,
        root_request_id=root_request_id,
        root_request_hash=root_request_hash,
        checkpoints=(*(lineage.checkpoints if lineage else ()), item),
        artifact_ids=artifact_ids,
        artifact_hashes=artifact_hashes,
        created_at_utc=(lineage.created_at_utc if lineage else completed_at_utc),
        updated_at_utc=completed_at_utc,
    )


def _proposal_from_orchestration(
    workflow_id: str,
    orchestration: TechnicalAgentOrchestration,
) -> ShadowForecastProposal:
    validations = {item.candidate_id: item for item in orchestration.validation_results}
    audits = {item.candidate_id: item for item in orchestration.evidence_audits}
    candidates = orchestration.candidates
    eligible = tuple(item.candidate_id for item in candidates if validations[item.candidate_id].selection_eligible)
    ineligible = tuple(item.candidate_id for item in candidates if item.candidate_id not in eligible)
    return ShadowForecastProposal.create(
        proposal_id="",
        workflow_id=workflow_id,
        technical_orchestration_id=orchestration.orchestration_id,
        technical_orchestration_hash=orchestration.content_hash,
        technical_request_id=orchestration.request.request_id,
        technical_request_hash=orchestration.request.content_hash,
        source_analysis_run_id=orchestration.request.source_analysis_run_id,
        source_analysis_run_hash=orchestration.request.source_analysis_run_content_hash,
        source_degree_resolution_id=orchestration.request.source_degree_resolution_id,
        source_degree_resolution_hash=orchestration.request.source_degree_resolution_content_hash,
        analysis_cutoff_utc=orchestration.request.analysis_cutoff_utc,
        candidate_ids=tuple(item.candidate_id for item in candidates),
        candidate_hashes={item.candidate_id: item.content_hash for item in candidates},
        validation_ids={key: item.validation_id for key, item in validations.items()},
        validation_hashes={key: item.content_hash for key, item in validations.items()},
        audit_ids={key: item.audit_id for key, item in audits.items()},
        audit_hashes={key: item.content_hash for key, item in audits.items()},
        eligible_candidate_ids=eligible,
        ineligible_candidate_ids=ineligible,
        proposed_selected_candidate_id=orchestration.shadow_resolution.selected_candidate_id,
        retrieved_lesson_ids=tuple(dict.fromkeys(item.lesson_id for item in orchestration.retrieved_lessons)),
        retrieved_lesson_hashes={item.retrieval_id: item.content_hash for item in orchestration.retrieved_lessons},
        candidate_freeze_hash=canonical_sha256([item.content_hash for item in candidates]),
        hard_validation_hash=canonical_sha256([item.content_hash for item in orchestration.validation_results]),
        stage_trace=orchestration.stage_trace,
        human_action_required=bool(eligible),
        created_at_utc=orchestration.created_at_utc,
    )


def _active_node(candidate: CandidateWaveCount) -> Mapping[str, Any]:
    hierarchy = candidate.degree_resolution.get("degree_hierarchy", ())
    nodes = tuple(item for item in hierarchy if isinstance(item, Mapping))
    active = candidate.degree_resolution.get("active_position")
    active_ids = set(active.get("wave_ids", ())) if isinstance(active, Mapping) else set()
    selected = next((item for item in reversed(nodes) if item.get("wave_id") in active_ids), None)
    return selected or (nodes[-1] if nodes else MappingProxyType({}))


def _candidate_direction(candidate: CandidateWaveCount) -> ForecastDirection:
    try:
        return ForecastDirection(str(_active_node(candidate).get("direction", "unknown")).casefold())
    except ValueError:
        return ForecastDirection.UNKNOWN


def _candidate_evidence(
    candidates: Sequence[CandidateWaveCount],
    validations: Mapping[str, CandidateValidationResult],
    audits: Mapping[str, EvidenceAuditResult],
) -> tuple[ForecastEvidence, ...]:
    evidence: list[ForecastEvidence] = []
    for candidate in candidates:
        hypothesis_id = candidate.candidate_id
        node = _active_node(candidate)
        wave_ids = (str(node["wave_id"]),) if node.get("wave_id") else ()
        validation = validations[hypothesis_id]
        structural_status = (
            ForecastEvidenceStatus.SUPPORTING
            if validation.structurally_valid
            else ForecastEvidenceStatus.CONTRADICTORY
        )
        evidence.append(
            ForecastEvidence(
                evidence_id="forecast_evidence_" + canonical_sha256({"candidate": hypothesis_id, "validation": validation.content_hash})[:24],
                hypothesis_ids=(hypothesis_id,),
                kind=ForecastEvidenceKind.ELLIOTT_RULE,
                status=structural_status,
                statement=("Deterministic Elliott hard-rule validation passed." if validation.structurally_valid else "Deterministic Elliott hard-rule validation failed."),
                rule_class=EvidenceRuleClass.HARD_STRUCTURAL,
                source_wave_ids=wave_ids,
                reason=(None if validation.structurally_valid else " ".join((*validation.shape_errors, *validation.hard_rule_errors, *validation.reference_errors, *validation.cutoff_errors))),
            )
        )
        audit = audits.get(hypothesis_id)
        if audit is None:
            evidence.append(
                ForecastEvidence(
                    evidence_id="forecast_evidence_" + canonical_sha256({"candidate": hypothesis_id, "audit": "unavailable"})[:24],
                    hypothesis_ids=(hypothesis_id,),
                    kind=ForecastEvidenceKind.DATA_QUALITY,
                    status=ForecastEvidenceStatus.UNAVAILABLE,
                    statement="The Phase 11D1 evidence audit was unavailable.",
                    reason="No immutable evidence-audit result was produced.",
                    source_wave_ids=wave_ids,
                )
            )
            continue
        for status, identifiers in (
            (ForecastEvidenceStatus.SUPPORTING, audit.supporting_evidence_ids),
            (ForecastEvidenceStatus.CONTRADICTORY, audit.contradictory_evidence_ids),
            (ForecastEvidenceStatus.NEUTRAL, audit.neutral_evidence_ids),
        ):
            for source_id in identifiers:
                evidence.append(
                    ForecastEvidence(
                        evidence_id="forecast_evidence_" + canonical_sha256({"candidate": hypothesis_id, "source": source_id, "status": status.value})[:24],
                        hypothesis_ids=(hypothesis_id,),
                        kind=ForecastEvidenceKind.OTHER_TECHNICAL,
                        status=status,
                        statement=f"Phase 11D1 classified technical source {source_id} as {status.value}.",
                        comparison_reference=source_id,
                        source_wave_ids=wave_ids,
                    )
                )
        for status, statements in (
            (ForecastEvidenceStatus.UNAVAILABLE, audit.unavailable_evidence),
            (ForecastEvidenceStatus.INCOMPARABLE, audit.incomparable_evidence),
        ):
            for statement in statements:
                evidence.append(
                    ForecastEvidence(
                        evidence_id="forecast_evidence_" + canonical_sha256({"candidate": hypothesis_id, "statement": statement, "status": status.value})[:24],
                        hypothesis_ids=(hypothesis_id,),
                        kind=ForecastEvidenceKind.DATA_QUALITY,
                        status=status,
                        statement=statement,
                        reason=statement,
                        source_wave_ids=wave_ids,
                    )
                )
    return tuple(evidence)


def _single_or_mixed(values: Sequence[Any], fallback: Any = "mixed") -> Any:
    normalized = {canonical_sha256(item): item for item in values}
    return next(iter(normalized.values())) if len(normalized) == 1 else fallback


def _build_forecast(
    run: Mapping[str, Any],
    resolution: Mapping[str, Any],
    orchestration: TechnicalAgentOrchestration,
    proposal: ShadowForecastProposal,
    approval: OperatorApproval,
) -> ForecastRecord:
    candidates = {item.candidate_id: item for item in orchestration.candidates}
    validations = {item.candidate_id: item for item in orchestration.validation_results}
    audits = {item.candidate_id: item for item in orchestration.evidence_audits}
    selected = candidates[approval.selected_candidate_id]
    eligible_candidates = tuple(candidates[item] for item in proposal.eligible_candidate_ids)
    known_hypotheses = {item.candidate_id for item in eligible_candidates}
    claim_hypotheses = {
        *(item.hypothesis_id for item in approval.target_claims),
        *(item.hypothesis_id for item in approval.invalidation_claims),
        *(item.hypothesis_id for item in approval.confirmation_claims),
        *(item.hypothesis_id for item in approval.expected_completion_windows),
    }
    unknown_claims = sorted(claim_hypotheses - known_hypotheses)
    if unknown_claims:
        raise ValueError("Operator claims reference ineligible or unknown candidates: " + ", ".join(unknown_claims) + ".")
    if _candidate_direction(selected) not in {ForecastDirection.UNKNOWN, approval.direction}:
        raise ValueError("Operator direction contradicts the selected frozen candidate.")

    evidence = _candidate_evidence(eligible_candidates, validations, audits)
    evidence_by_hypothesis: dict[str, tuple[str, ...]] = {
        candidate.candidate_id: tuple(item.evidence_id for item in evidence if candidate.candidate_id in item.hypothesis_ids)
        for candidate in eligible_candidates
    }
    target_ids = {candidate.candidate_id: tuple(item.claim_id for item in approval.target_claims if item.hypothesis_id == candidate.candidate_id) for candidate in eligible_candidates}
    invalidation_ids = {candidate.candidate_id: tuple(item.claim_id for item in approval.invalidation_claims if item.hypothesis_id == candidate.candidate_id) for candidate in eligible_candidates}
    confirmation_ids = {candidate.candidate_id: tuple(item.claim_id for item in approval.confirmation_claims if item.hypothesis_id == candidate.candidate_id) for candidate in eligible_candidates}
    window_ids = {candidate.candidate_id: tuple(item.window_id for item in approval.expected_completion_windows if item.hypothesis_id == candidate.candidate_id) for candidate in eligible_candidates}

    def dimensions(candidate: CandidateWaveCount) -> tuple[str, str, str, str]:
        node = _active_node(candidate)
        return (
            str(node.get("degree") or "Unassigned"),
            str(node.get("sequence_position") or node.get("wave") or "Unassigned"),
            structure_family(node.get("structure")) or str(node.get("structure") or "unresolved"),
            str(node.get("completion_status") or candidate.degree_resolution.get("resolution_status") or "unresolved"),
        )

    selected_dimensions = dimensions(selected)
    main = MainHypothesis(
        hypothesis_id=selected.candidate_id,
        label="Human-selected shadow candidate",
        direction=approval.direction,
        degree=selected_dimensions[0],
        wave_label=selected_dimensions[1],
        pattern_family=selected_dimensions[2],
        completion_status=selected_dimensions[3],
        summary=" ".join(selected.concise_reasoning_summary),
        count=selected.degree_resolution,
        target_claim_ids=target_ids[selected.candidate_id],
        invalidation_claim_ids=invalidation_ids[selected.candidate_id],
        confirmation_claim_ids=confirmation_ids[selected.candidate_id],
        completion_window_ids=window_ids[selected.candidate_id],
        evidence_ids=evidence_by_hypothesis[selected.candidate_id],
        original_uncalibrated_confidence=selected.degree_resolution.get("confidence", {}),
    )
    alternatives: list[AlternativeHypothesis] = []
    for candidate in eligible_candidates:
        if candidate.candidate_id == selected.candidate_id:
            continue
        degree, wave, family, completion = dimensions(candidate)
        alternatives.append(
            AlternativeHypothesis(
                hypothesis_id=candidate.candidate_id,
                label="Independent frozen alternative",
                direction=_candidate_direction(candidate),
                degree=degree,
                wave_label=wave,
                pattern_family=family,
                completion_status=completion,
                summary=" ".join(candidate.concise_reasoning_summary),
                count=candidate.degree_resolution,
                activation_conditions=(),
                distinguishing_evidence_needed=candidate.distinguishing_features,
                target_claim_ids=target_ids[candidate.candidate_id],
                invalidation_claim_ids=invalidation_ids[candidate.candidate_id],
                confirmation_claim_ids=confirmation_ids[candidate.candidate_id],
                completion_window_ids=window_ids[candidate.candidate_id],
                evidence_ids=evidence_by_hypothesis[candidate.candidate_id],
                original_uncalibrated_confidence=candidate.degree_resolution.get("confidence", {}),
            )
        )

    market_data = run.get("evidence", {}).get("market_data", ()) if isinstance(run.get("evidence"), Mapping) else ()
    datasets = tuple(
        dataset_cutoff_from_stored_summary(item, requested_symbol=orchestration.request.symbol)
        for item in market_data
        if isinstance(item, Mapping)
    )
    if not datasets:
        raise ValueError("Forecast approval requires stored decision-time dataset summaries.")
    provisional = any(not item.completed_candles_only for item in datasets) or str(selected.degree_resolution.get("resolution_status", "")).casefold() == "provisional"
    first_target = next(item for item in approval.target_claims if item.hypothesis_id == selected.candidate_id)
    first_invalidation = next(
        (
            item
            for item in approval.invalidation_claims
            if item.hypothesis_id == selected.candidate_id
            and item.price_level is not None
        ),
        None,
    )
    first_window = next(item for item in approval.expected_completion_windows if item.hypothesis_id == selected.candidate_id)
    source_hashes = {
        "analysis_run": stored_record_content_hash(run),
        "degree_resolution": stored_record_content_hash(resolution),
        "technical_agent_orchestration": orchestration.content_hash,
        "shadow_forecast_proposal": proposal.content_hash,
        "operator_approval": approval.content_hash,
    }
    premise = FrozenTechnicalPremise.create(
        resolution_id=str(orchestration.request.source_degree_resolution_id),
        symbol=orchestration.request.symbol,
        exchange=_single_or_mixed([item.exchange for item in datasets]),
        analysed_at=orchestration.created_at_utc,
        market_data_cutoff=orchestration.request.analysis_cutoff_utc,
        technical_structure={
            "selected_candidate": selected.degree_resolution,
            "deterministic_validation": validations[selected.candidate_id].to_dict(),
            "evidence_audit": (audits[selected.candidate_id].to_dict() if selected.candidate_id in audits else None),
            "independent_alternatives": [item.degree_resolution for item in eligible_candidates if item.candidate_id != selected.candidate_id],
            "phase11d1_orchestration_id": orchestration.orchestration_id,
        },
        direction=ScenarioDirection(approval.direction.value),
        current_wave_or_phase=f"{selected_dimensions[0]} {selected_dimensions[1]}",
        target_low=first_target.target_low,
        target_high=first_target.target_high,
        invalidation_level=(
            first_invalidation.price_level if first_invalidation is not None else None
        ),
        expected_completion_start=first_window.start_utc,
        expected_completion_end=first_window.end_utc,
        technical_confidence=selected.degree_resolution.get("confidence", {}),
        technical_readiness={
            "structurally_valid": validations[selected.candidate_id].structurally_valid,
            "selection_eligible": validations[selected.candidate_id].selection_eligible,
            "human_approved": True,
        },
        supporting_technical_evidence=tuple(item.statement for item in evidence if item.status is ForecastEvidenceStatus.SUPPORTING and selected.candidate_id in item.hypothesis_ids),
        alternative_technical_hypotheses=tuple(item.summary for item in alternatives),
        source_hashes=source_hashes,
    )
    return ForecastRecord.create(
        forecast_id="",
        forecast_version=1,
        supersedes_forecast_id=None,
        source_analysis_run_id=orchestration.request.source_analysis_run_id,
        source_degree_resolution_id=orchestration.request.source_degree_resolution_id,
        created_at_utc=approval.approved_at_utc,
        analysis_cutoff_utc=orchestration.request.analysis_cutoff_utc,
        symbol=orchestration.request.symbol,
        exchange=_single_or_mixed([item.exchange for item in datasets]),
        provider=orchestration.provider,
        model=orchestration.model or "unavailable",
        feed=_single_or_mixed([item.feed_identity for item in datasets]),
        session=_single_or_mixed([item.session for item in datasets]),
        timezone=_single_or_mixed([item.timezone for item in datasets]),
        adjustment=_single_or_mixed([item.adjustment for item in datasets], {"state": "mixed"}),
        price_basis=_single_or_mixed([item.price_basis for item in datasets]),
        dataset_cutoffs=datasets,
        frozen_technical_premise=premise,
        main_hypothesis=main,
        alternative_hypotheses=tuple(alternatives),
        target_claims=approval.target_claims,
        invalidation_claims=approval.invalidation_claims,
        confirmation_claims=approval.confirmation_claims,
        expected_completion_windows=approval.expected_completion_windows,
        evidence=evidence,
        evaluation_eligibility=EvaluationEligibility(
            status=EvaluationEligibilityStatus.EVALUABLE,
            record_state=(ForecastRecordState.PROVISIONAL if provisional else ForecastRecordState.FINAL),
            reason_codes=(("source_resolution_provisional",) if provisional else ()),
            typed_claims_present=True,
            price_accuracy_evaluable=True,
            timing_accuracy_evaluable=True,
        ),
        provider_versions={"technical_agents": orchestration.provider},
        model_versions={"technical_agents": orchestration.model or "unavailable"},
        prompt_versions={**dict(orchestration.prompt_versions), "operator_approval": OPERATOR_APPROVAL_SCHEMA_VERSION},
        policy_versions={"forecast_ledger": "forecast-evaluation-policy-1.0.0", "technical_agents": orchestration.policy_version, "shadow_workflow": SHADOW_WORKFLOW_POLICY_VERSION},
        original_uncalibrated_confidence=selected.degree_resolution.get("confidence", {}),
        source_hashes=source_hashes,
    )


def _result_from(
    previous: ShadowWorkflowResult | None,
    *,
    workflow_id: str,
    status: ShadowWorkflowStatus,
    current_stage: ShadowWorkflowStage,
    lineage: ShadowWorkflowLineage,
    updated_at_utc: str,
    **changes: Any,
) -> ShadowWorkflowResult:
    base: dict[str, Any] = {
        "workflow_id": workflow_id,
        "status": status,
        "current_stage": current_stage,
        "lineage": lineage,
        "proposal": None,
        "operator_approval": None,
        "forecast_id": None,
        "forecast_hash": None,
        "observation_set_id": None,
        "observation_set_hash": None,
        "evaluation_id": None,
        "evaluation_hash": None,
        "outcome_review_draft": None,
        "human_review_id": None,
        "human_review_hash": None,
        "mistake_lesson_proposal_draft": None,
        "future_technical_orchestration_id": None,
        "future_technical_orchestration_hash": None,
        "human_gate": None,
        "warnings": (),
        "errors": (),
        "created_at_utc": updated_at_utc,
        "updated_at_utc": updated_at_utc,
        "versions": {
            "workflow": SHADOW_WORKFLOW_SCHEMA_VERSION,
            "policy": SHADOW_WORKFLOW_POLICY_VERSION,
            "calculation": SHADOW_WORKFLOW_CALCULATION_VERSION,
        },
    }
    if previous is not None:
        payload = previous.to_dict()
        payload.pop("content_hash")
        base.update(payload)
        base.update(
            {
                "status": status,
                "current_stage": current_stage,
                "lineage": lineage,
                "updated_at_utc": updated_at_utc,
                "human_gate": None,
            }
        )
    base.update(changes)
    return ShadowWorkflowResult.create(**base)


class Phase11ShadowWorkflow:
    """Compose all six approved agents behind explicit offline human gates."""

    def __init__(
        self,
        *,
        store: Any,
        technical_orchestrator: TechnicalAgentOrchestrator,
        outcome_learning_orchestrator: OutcomeLearningAgentOrchestrator,
    ) -> None:
        required = (
            "get_run",
            "get_degree_resolution",
            "create_forecast_agent_orchestration",
            "create_forecast_record",
            "create_forecast_observation_set",
            "create_forecast_outcome_evaluation",
            "create_forecast_outcome_learning_orchestration",
        )
        missing = [name for name in required if not callable(getattr(store, name, None))]
        if missing:
            raise TypeError("store lacks required Phase 11 APIs: " + ", ".join(missing) + ".")
        self.store = store
        self.technical_orchestrator = technical_orchestrator
        self.outcome_learning_orchestrator = outcome_learning_orchestrator

    @staticmethod
    def _previous(result: ShadowWorkflowResult, expected: ShadowWorkflowStage) -> ShadowWorkflowResult:
        result = replay_shadow_workflow_result(result)
        if result.current_stage is not expected:
            raise RuntimeError(
                f"Out-of-order transition: expected {expected.value}, found {result.current_stage.value}."
            )
        return result

    def start_decision_time_analysis(
        self,
        request: TechnicalAgentRequest,
        *,
        executed_at_utc: str,
        shadow_mode: bool = False,
        persist: bool = True,
    ) -> ShadowWorkflowResult:
        if not shadow_mode or not request.shadow_mode:
            raise RuntimeError("Phase 11E1 requires shadow_mode=True at request and invocation boundaries.")
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean.")
        timestamp = _normalize_utc(executed_at_utc, field_name="executed_at_utc")
        resolution = self.store.get_degree_resolution(request.source_degree_resolution_id)
        if resolution is None:
            raise KeyError("Source degree resolution is not registered.")
        orchestration = self.technical_orchestrator.orchestrate(
            request,
            existing_degree_resolution=resolution,
            shadow_mode=True,
            executed_at_utc=timestamp,
        )
        if persist:
            self.store.create_forecast_agent_orchestration(orchestration)
        workflow_id = "shadow_workflow_" + canonical_sha256({"request": request.content_hash})[:32]
        proposal = _proposal_from_orchestration(workflow_id, orchestration)
        lineage = _checkpoint(
            None,
            workflow_id=workflow_id,
            root_request_id=request.request_id,
            root_request_hash=request.content_hash,
            stage=ShadowWorkflowStage.DECISION_TIME_ANALYSIS,
            input_hashes={"technical_request": request.content_hash},
            output_ids={"technical_orchestration": orchestration.orchestration_id, "forecast_proposal": proposal.proposal_id},
            output_hashes={"technical_orchestration": orchestration.content_hash, "forecast_proposal": proposal.content_hash},
            human_action_required=proposal.human_action_required,
            completed_at_utc=timestamp,
        )
        gate = None
        status = ShadowWorkflowStatus.FAILED
        errors = orchestration.errors
        if proposal.human_action_required:
            status = ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED
            gate = HumanActionGate.create(
                gate_id="",
                workflow_id=workflow_id,
                stage=ShadowWorkflowStage.DECISION_TIME_ANALYSIS,
                required_action="approve_forecast",
                reason="A human must select an eligible frozen candidate and supply typed claims.",
                source_ids={"forecast_proposal": proposal.proposal_id},
                source_hashes={"forecast_proposal": proposal.content_hash},
                created_at_utc=timestamp,
            )
        elif not errors:
            errors = ("No hard-rule-valid candidate is available for operator approval.",)
        return _result_from(
            None,
            workflow_id=workflow_id,
            status=status,
            current_stage=ShadowWorkflowStage.DECISION_TIME_ANALYSIS,
            lineage=lineage,
            updated_at_utc=timestamp,
            proposal=proposal,
            human_gate=gate,
            warnings=orchestration.warnings,
            errors=errors,
        )

    def approve_forecast(
        self,
        previous: ShadowWorkflowResult,
        approval: OperatorApproval,
        *,
        persist: bool = True,
    ) -> ShadowWorkflowResult:
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean.")
        previous = self._previous(previous, ShadowWorkflowStage.DECISION_TIME_ANALYSIS)
        proposal = previous.proposal
        if proposal is None or not proposal.human_action_required:
            raise RuntimeError("The proposal has no approvable candidate.")
        if approval.proposal_id != proposal.proposal_id or approval.proposal_hash != proposal.content_hash:
            raise ValueError("Operator approval references the wrong proposal ID or hash.")
        if approval.selected_candidate_id not in proposal.eligible_candidate_ids:
            raise ValueError("A hard-rule-invalid or ineligible candidate cannot be approved.")
        if _utc(approval.approved_at_utc) < _utc(proposal.created_at_utc):
            raise ValueError("Operator approval cannot predate the frozen proposal.")
        orchestration = self.store.get_forecast_agent_orchestration(proposal.technical_orchestration_id)
        if orchestration is None or orchestration.content_hash != proposal.technical_orchestration_hash:
            raise ValueError("Stored D1 orchestration differs from the proposal lineage.")
        if validate_technical_agent_orchestration(orchestration):
            raise ValueError("Stored D1 orchestration failed immutable replay validation.")
        run = self.store.get_run(proposal.source_analysis_run_id)
        resolution = self.store.get_degree_resolution(proposal.source_degree_resolution_id)
        if run is None or resolution is None:
            raise KeyError("Forecast source records are not registered.")
        forecast = _build_forecast(run, resolution, orchestration, proposal, approval)
        if forecast.content_hash != forecast_record_content_hash(forecast) or validate_forecast_record(forecast):
            raise ValueError("Approved ForecastRecord failed immutable validation.")
        if persist:
            self.store.create_forecast_record(forecast)
        timestamp = approval.approved_at_utc
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.HUMAN_FORECAST_APPROVAL,
            input_hashes={"forecast_proposal": proposal.content_hash, "operator_approval": approval.content_hash},
            output_ids={"forecast_record": forecast.forecast_id},
            output_hashes={"forecast_record": forecast.content_hash},
            human_action_required=False,
            completed_at_utc=timestamp,
        )
        gate = HumanActionGate.create(
            gate_id="",
            workflow_id=previous.workflow_id,
            stage=ShadowWorkflowStage.AWAITING_OBSERVATIONS,
            required_action="supply_immutable_observation_set",
            reason="Future candles must be supplied explicitly through the Phase 11B API.",
            source_ids={"forecast_record": forecast.forecast_id},
            source_hashes={"forecast_record": forecast.content_hash},
            created_at_utc=timestamp,
        )
        lineage = _checkpoint(
            lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.AWAITING_OBSERVATIONS,
            input_hashes={"forecast_record": forecast.content_hash},
            output_ids={"human_gate": gate.gate_id},
            output_hashes={"human_gate": gate.content_hash},
            human_action_required=True,
            completed_at_utc=timestamp,
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=ShadowWorkflowStatus.AWAITING_OBSERVATIONS,
            current_stage=ShadowWorkflowStage.AWAITING_OBSERVATIONS,
            lineage=lineage,
            updated_at_utc=timestamp,
            operator_approval=approval,
            forecast_id=forecast.forecast_id,
            forecast_hash=forecast.content_hash,
            human_gate=gate,
        )

    def accept_observations(
        self,
        previous: ShadowWorkflowResult,
        observation_set: ForecastObservationSet,
        *,
        accepted_at_utc: str,
        persist: bool = True,
    ) -> ShadowWorkflowResult:
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean.")
        previous = self._previous(previous, ShadowWorkflowStage.AWAITING_OBSERVATIONS)
        timestamp = _normalize_utc(accepted_at_utc, field_name="accepted_at_utc")
        forecast = self.store.get_forecast_record(previous.forecast_id)
        if forecast is None or forecast.content_hash != previous.forecast_hash:
            raise ValueError("Stored forecast differs from the workflow lineage.")
        errors = validate_forecast_observation_set(observation_set, forecast)
        if errors or observation_set.content_hash != forecast_observation_set_content_hash(observation_set):
            raise ValueError("Invalid immutable observation set: " + " ".join(errors or ("content hash differs.",)))
        if any("path" in key.casefold() or "mutable" in key.casefold() for key in observation_set.source_hashes):
            raise ValueError("Mutable source-file references are not accepted by the shadow workflow.")
        if persist:
            self.store.create_forecast_observation_set(observation_set)
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.OBSERVATIONS_ACCEPTED,
            input_hashes={"forecast_record": forecast.content_hash, "observation_set": observation_set.content_hash},
            output_ids={"observation_set": observation_set.observation_set_id},
            output_hashes={"observation_set": observation_set.content_hash},
            human_action_required=False,
            completed_at_utc=timestamp,
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=ShadowWorkflowStatus.READY,
            current_stage=ShadowWorkflowStage.OBSERVATIONS_ACCEPTED,
            lineage=lineage,
            updated_at_utc=timestamp,
            observation_set_id=observation_set.observation_set_id,
            observation_set_hash=observation_set.content_hash,
        )

    def evaluate(
        self,
        previous: ShadowWorkflowResult,
        *,
        evaluated_at_utc: str,
        lower_timeframe_observation_sets: Sequence[ForecastObservationSet] = (),
        persist: bool = True,
    ) -> ShadowWorkflowResult:
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean.")
        previous = self._previous(previous, ShadowWorkflowStage.OBSERVATIONS_ACCEPTED)
        timestamp = _normalize_utc(evaluated_at_utc, field_name="evaluated_at_utc")
        forecast = self.store.get_forecast_record(previous.forecast_id)
        observation = self.store.get_forecast_observation_set(previous.observation_set_id)
        if forecast is None or observation is None:
            raise KeyError("Forecast or observation set is missing from persistence.")
        if forecast.content_hash != previous.forecast_hash or observation.content_hash != previous.observation_set_hash:
            raise ValueError("Stored evaluation inputs differ from workflow lineage.")
        evaluation = evaluate_forecast_observation_set(
            forecast,
            observation,
            lower_timeframe_observation_sets=lower_timeframe_observation_sets,
            created_at_utc=timestamp,
        )
        errors = validate_forecast_outcome_evaluation(
            evaluation,
            forecast=forecast,
            observation_set=observation,
            lower_timeframe_observation_sets=lower_timeframe_observation_sets,
        )
        if errors or evaluation.content_hash != forecast_outcome_evaluation_content_hash(evaluation):
            raise ValueError("Deterministic evaluation failed validation: " + " ".join(errors or ("content hash differs.",)))
        expected_alternatives = {item.hypothesis_id for item in forecast.alternative_hypotheses}
        actual_alternatives = {item.hypothesis_id for item in evaluation.alternative_hypothesis_outcomes}
        if expected_alternatives != actual_alternatives or evaluation.main_hypothesis_outcome.hypothesis_id != forecast.main_hypothesis.hypothesis_id:
            raise ValueError("Deterministic evaluation did not preserve main and alternative separation.")
        if persist:
            self.store.create_forecast_outcome_evaluation(evaluation)
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.DETERMINISTIC_EVALUATION,
            input_hashes={"forecast_record": forecast.content_hash, "observation_set": observation.content_hash},
            output_ids={"outcome_evaluation": evaluation.evaluation_id},
            output_hashes={"outcome_evaluation": evaluation.content_hash},
            human_action_required=False,
            completed_at_utc=timestamp,
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=ShadowWorkflowStatus.READY,
            current_stage=ShadowWorkflowStage.DETERMINISTIC_EVALUATION,
            lineage=lineage,
            updated_at_utc=timestamp,
            evaluation_id=evaluation.evaluation_id,
            evaluation_hash=evaluation.content_hash,
        )

    def draft_outcome_review(
        self,
        previous: ShadowWorkflowResult,
        *,
        drafted_at_utc: str,
        shadow_mode: bool = False,
        persist: bool = True,
    ) -> ShadowWorkflowResult:
        previous = self._previous(previous, ShadowWorkflowStage.DETERMINISTIC_EVALUATION)
        if not shadow_mode:
            raise RuntimeError("Outcome-review drafting requires explicit shadow_mode=True.")
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean.")
        timestamp = _normalize_utc(drafted_at_utc, field_name="drafted_at_utc")
        forecast = self.store.get_forecast_record(previous.forecast_id)
        observation = self.store.get_forecast_observation_set(previous.observation_set_id)
        evaluation = self.store.get_forecast_outcome_evaluation(previous.evaluation_id)
        if forecast is None or observation is None or evaluation is None:
            raise KeyError("Outcome-review inputs are missing from persistence.")
        provider = self.outcome_learning_orchestrator.provider
        request = build_outcome_reviewer_request(
            forecast,
            observation,
            evaluation,
            provider=str(getattr(provider, "name", "")),
            model=getattr(provider, "model", None),
            created_at_utc=timestamp,
            shadow_mode=True,
        )
        before_reviews = tuple(self.store.list_forecast_outcome_reviews(forecast_id=forecast.forecast_id, limit=10_000))
        orchestration = self.outcome_learning_orchestrator.run_outcome_reviewer(
            request,
            shadow_mode=True,
            executed_at_utc=timestamp,
        )
        if persist:
            self.store.create_forecast_outcome_learning_orchestration(orchestration)
        after_reviews = tuple(self.store.list_forecast_outcome_reviews(forecast_id=forecast.forecast_id, limit=10_000))
        if tuple(item.content_hash for item in before_reviews) != tuple(item.content_hash for item in after_reviews):
            raise RuntimeError("Outcome Reviewer attempted to mutate human review persistence.")
        draft = orchestration.outcome_review_draft
        if orchestration.status is OutcomeLearningOrchestrationStatus.FAILED or draft is None:
            lineage = _checkpoint(
                previous.lineage,
                workflow_id=previous.workflow_id,
                root_request_id=previous.lineage.root_request_id,
                root_request_hash=previous.lineage.root_request_hash,
                stage=ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT,
                input_hashes={"outcome_evaluation": evaluation.content_hash},
                output_ids={"outcome_reviewer_orchestration": orchestration.orchestration_id},
                output_hashes={"outcome_reviewer_orchestration": orchestration.content_hash},
                human_action_required=False,
                completed_at_utc=timestamp,
            )
            return _result_from(
                previous,
                workflow_id=previous.workflow_id,
                status=ShadowWorkflowStatus.FAILED,
                current_stage=ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT,
                lineage=lineage,
                updated_at_utc=timestamp,
                warnings=tuple((*previous.warnings, *orchestration.warnings)),
                errors=tuple((*previous.errors, *orchestration.validation_errors)),
            )
        gate = HumanActionGate.create(
            gate_id="",
            workflow_id=previous.workflow_id,
            stage=ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT,
            required_action="record_human_outcome_review",
            reason="The agent draft cannot create or approve a Phase 11C human review.",
            source_ids={"outcome_review_draft": draft.draft_id},
            source_hashes={"outcome_review_draft": draft.content_hash},
            created_at_utc=timestamp,
        )
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT,
            input_hashes={"outcome_evaluation": evaluation.content_hash},
            output_ids={"outcome_reviewer_orchestration": orchestration.orchestration_id, "outcome_review_draft": draft.draft_id},
            output_hashes={"outcome_reviewer_orchestration": orchestration.content_hash, "outcome_review_draft": draft.content_hash},
            human_action_required=True,
            completed_at_utc=timestamp,
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED,
            current_stage=ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT,
            lineage=lineage,
            updated_at_utc=timestamp,
            outcome_review_draft=draft,
            human_gate=gate,
            warnings=tuple((*previous.warnings, *orchestration.warnings)),
        )

    def accept_human_outcome_review(
        self,
        previous: ShadowWorkflowResult,
        review: ForecastOutcomeReview,
        *,
        require_persisted_review: bool = True,
    ) -> ShadowWorkflowResult:
        if not isinstance(require_persisted_review, bool):
            raise TypeError("require_persisted_review must be boolean.")
        previous = self._previous(previous, ShadowWorkflowStage.OUTCOME_REVIEW_DRAFT)
        stored = self.store.get_forecast_outcome_review(review.review_id)
        if require_persisted_review and (
            stored is None or stored.content_hash != review.content_hash
        ):
            raise ValueError("Human review must already exist unchanged in Phase 11C persistence.")
        if stored is not None and stored.content_hash != review.content_hash:
            raise ValueError("A stored human review with this ID has different content.")
        evaluation = self.store.get_forecast_outcome_evaluation(previous.evaluation_id)
        if evaluation is None or evaluation.content_hash != previous.evaluation_hash:
            raise ValueError("Stored evaluation differs from workflow lineage.")
        parent = self.store.get_forecast_outcome_review(review.parent_review_id) if review.parent_review_id else None
        errors = validate_forecast_outcome_review(review, evaluation=evaluation, parent_review=parent)
        if errors:
            raise ValueError("Human review lineage failed validation: " + " ".join(errors))
        if review.decision not in {ReviewDecision.APPROVED, ReviewDecision.REVISED}:
            raise ValueError("Only an approved or revised human review can continue E1.")
        if review.forecast_id != previous.forecast_id or review.evaluation_id != previous.evaluation_id:
            raise ValueError("Human review belongs to another forecast or evaluation.")
        if review.deterministic_scoring_error:
            raise ValueError("A review awaiting deterministic correction cannot continue.")
        timestamp = review.reviewed_at_utc
        eligible_for_learning = review.reviewed_outcome_status in {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}
        stopped = review.reviewed_outcome_status in {OutcomeStatus.UNRESOLVED, OutcomeStatus.INSUFFICIENT_DATA, OutcomeStatus.INCOMPARABLE}
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW,
            input_hashes={"outcome_evaluation": evaluation.content_hash, "human_outcome_review": review.content_hash},
            output_ids={"human_outcome_review": review.review_id},
            output_hashes={"human_outcome_review": review.content_hash},
            human_action_required=eligible_for_learning,
            completed_at_utc=timestamp,
        )
        if eligible_for_learning:
            gate = HumanActionGate.create(
                gate_id="",
                workflow_id=previous.workflow_id,
                stage=ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW,
                required_action="request_mistake_memory_proposal",
                reason="A failed or partial reviewed outcome may be drafted as a lesson proposal, but never learned automatically.",
                source_ids={"human_outcome_review": review.review_id},
                source_hashes={"human_outcome_review": review.content_hash},
                created_at_utc=timestamp,
            )
            return _result_from(
                previous,
                workflow_id=previous.workflow_id,
                status=ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED,
                current_stage=ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW,
                lineage=lineage,
                updated_at_utc=timestamp,
                human_review_id=review.review_id,
                human_review_hash=review.content_hash,
                human_gate=gate,
            )
        lineage = _checkpoint(
            lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.COMPLETED,
            input_hashes={"human_outcome_review": review.content_hash},
            output_ids={"human_outcome_review": review.review_id},
            output_hashes={"human_outcome_review": review.content_hash},
            human_action_required=False,
            completed_at_utc=timestamp,
        )
        warning = (
            "Outcome is unresolved, insufficient or incomparable; mistake learning stopped."
            if stopped
            else "Successful reviewed forecast completed without a mistake proposal."
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=(ShadowWorkflowStatus.STOPPED_NO_LEARNING if stopped else ShadowWorkflowStatus.COMPLETED),
            current_stage=ShadowWorkflowStage.COMPLETED,
            lineage=lineage,
            updated_at_utc=timestamp,
            human_review_id=review.review_id,
            human_review_hash=review.content_hash,
            warnings=tuple((*previous.warnings, warning)),
        )

    def _active_memory(self, cutoff_utc: str) -> tuple[tuple[MistakeMemoryLesson, ...], tuple[LessonSource, ...], tuple[LessonEvent, ...]]:
        lessons = tuple(self.store.list_mistake_memory_lessons(limit=10_000))
        sources = tuple(self.store.list_mistake_memory_sources(limit=10_000))
        events = tuple(self.store.list_mistake_memory_events(limit=10_000))
        active: list[MistakeMemoryLesson] = []
        selected_events: list[LessonEvent] = []
        for lesson in lessons:
            if _utc(lesson.proposed_at_utc) > _utc(cutoff_utc):
                continue
            prefix = tuple(item for item in events if item.lesson_id == lesson.lesson_id and _utc(item.recorded_at_utc) <= _utc(cutoff_utc))
            if validate_lesson_event_chain(lesson, prefix):
                continue
            if lesson_current_status(lesson, prefix, at_or_before_utc=cutoff_utc) is LessonStatus.ACTIVE:
                active.append(lesson)
                selected_events.extend(prefix)
        active_ids = {item.lesson_id for item in active}
        selected_sources = tuple(item for item in sources if item.lesson_id in active_ids and _utc(item.added_at_utc) <= _utc(cutoff_utc))
        return tuple(active), selected_sources, tuple(selected_events)

    def propose_mistake_memory(
        self,
        previous: ShadowWorkflowResult,
        *,
        proposed_at_utc: str,
        shadow_mode: bool = False,
        additional_supporting_bundles: Sequence[tuple[ForecastRecord, ForecastOutcomeEvaluation, ForecastOutcomeReview]] = (),
        counterexample_bundles: Sequence[tuple[ForecastRecord, ForecastOutcomeEvaluation, ForecastOutcomeReview]] = (),
        existing_lesson_reference_id: str | None = None,
        persist: bool = True,
    ) -> ShadowWorkflowResult:
        previous = self._previous(previous, ShadowWorkflowStage.HUMAN_OUTCOME_REVIEW)
        if not shadow_mode:
            raise RuntimeError("Mistake-memory drafting requires explicit shadow_mode=True.")
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean.")
        timestamp = _normalize_utc(proposed_at_utc, field_name="proposed_at_utc")
        forecast = self.store.get_forecast_record(previous.forecast_id)
        evaluation = self.store.get_forecast_outcome_evaluation(previous.evaluation_id)
        review = self.store.get_forecast_outcome_review(previous.human_review_id)
        if forecast is None or evaluation is None or review is None:
            raise KeyError("Mistake-proposal inputs are missing from persistence.")
        if review.content_hash != previous.human_review_hash or evaluation.content_hash != previous.evaluation_hash:
            raise ValueError("Mistake-proposal inputs differ from workflow lineage.")
        if review.reviewed_outcome_status not in {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}:
            raise ValueError("Only failed or partial reviewed outcomes can reach mistake drafting.")
        lessons, sources, events = self._active_memory(timestamp)
        provider = self.outcome_learning_orchestrator.provider
        request = build_mistake_memory_proposal_request(
            forecast,
            evaluation,
            review,
            provider=str(getattr(provider, "name", "")),
            model=getattr(provider, "model", None),
            created_at_utc=timestamp,
            additional_supporting_bundles=additional_supporting_bundles,
            counterexample_bundles=counterexample_bundles,
            existing_lessons=lessons,
            existing_lesson_sources=sources,
            existing_lesson_events=events,
            existing_lesson_reference_id=existing_lesson_reference_id,
            shadow_mode=True,
        )
        before = (
            tuple(item.content_hash for item in self.store.list_mistake_memory_lessons(limit=10_000)),
            tuple(item.content_hash for item in self.store.list_mistake_memory_sources(limit=10_000)),
            tuple(item.content_hash for item in self.store.list_mistake_memory_events(limit=10_000)),
        )
        orchestration = self.outcome_learning_orchestrator.run_mistake_memory_proposal(
            request,
            shadow_mode=True,
            executed_at_utc=timestamp,
        )
        if persist:
            self.store.create_forecast_outcome_learning_orchestration(orchestration)
        after = (
            tuple(item.content_hash for item in self.store.list_mistake_memory_lessons(limit=10_000)),
            tuple(item.content_hash for item in self.store.list_mistake_memory_sources(limit=10_000)),
            tuple(item.content_hash for item in self.store.list_mistake_memory_events(limit=10_000)),
        )
        if before != after:
            raise RuntimeError("Mistake Memory Proposal Agent mutated lesson persistence.")
        draft = orchestration.mistake_lesson_proposal_draft
        human_required = draft is not None and orchestration.status is not OutcomeLearningOrchestrationStatus.FAILED
        gate = None
        if human_required:
            gate = HumanActionGate.create(
                gate_id="",
                workflow_id=previous.workflow_id,
                stage=ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL,
                required_action="review_create_and_activate_lesson_manually",
                reason="The proposal is advisory and cannot create, approve or activate a lesson.",
                source_ids={"mistake_lesson_proposal_draft": draft.draft_id},
                source_hashes={"mistake_lesson_proposal_draft": draft.content_hash},
                created_at_utc=timestamp,
            )
        output_ids = {"mistake_proposal_orchestration": orchestration.orchestration_id}
        output_hashes = {"mistake_proposal_orchestration": orchestration.content_hash}
        if draft is not None:
            output_ids["mistake_lesson_proposal_draft"] = draft.draft_id
            output_hashes["mistake_lesson_proposal_draft"] = draft.content_hash
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL,
            input_hashes={"forecast_record": forecast.content_hash, "outcome_evaluation": evaluation.content_hash, "human_outcome_review": review.content_hash},
            output_ids=output_ids,
            output_hashes=output_hashes,
            human_action_required=human_required,
            completed_at_utc=timestamp,
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=(ShadowWorkflowStatus.HUMAN_ACTION_REQUIRED if human_required else ShadowWorkflowStatus.FAILED),
            current_stage=ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL,
            lineage=lineage,
            updated_at_utc=timestamp,
            mistake_lesson_proposal_draft=draft,
            human_gate=gate,
            warnings=tuple((*previous.warnings, *orchestration.warnings)),
            errors=tuple((*previous.errors, *orchestration.validation_errors)),
        )

    def prove_future_analysis_isolation(
        self,
        previous: ShadowWorkflowResult,
        request: TechnicalAgentRequest,
        *,
        expected_lesson_id: str,
        phase5_ranking_hash_before: str,
        phase5_ranking_hash_after: str,
        executed_at_utc: str,
        shadow_mode: bool = False,
    ) -> ShadowWorkflowResult:
        previous = self._previous(previous, ShadowWorkflowStage.MISTAKE_MEMORY_PROPOSAL)
        if not shadow_mode or not request.shadow_mode:
            raise RuntimeError("Future-analysis proof requires explicit shadow_mode=True.")
        before_hash = _require_hash(phase5_ranking_hash_before, field_name="phase5_ranking_hash_before")
        after_hash = _require_hash(phase5_ranking_hash_after, field_name="phase5_ranking_hash_after")
        if before_hash != after_hash:
            raise ValueError("Phase 5 analogue ranking changed while mistake memory was introduced.")
        forecast = self.store.get_forecast_record(previous.forecast_id)
        lesson = self.store.get_mistake_memory_lesson(expected_lesson_id)
        if forecast is None or lesson is None:
            raise KeyError("Future proof requires the source forecast and human-created lesson.")
        if lesson.scope.scope_type.value != "exact_case" or forecast.forecast_id not in lesson.scope.exact_forecast_ids:
            raise ValueError("Future proof requires an active exact-case lesson for this forecast.")
        lesson_events = tuple(self.store.list_mistake_memory_events(lesson_id=lesson.lesson_id, limit=10_000))
        if lesson_current_status(lesson, lesson_events, at_or_before_utc=request.analysis_cutoff_utc) is not LessonStatus.ACTIVE:
            raise ValueError("The exact-case lesson was not active by the future analysis cutoff.")
        sources = tuple(self.store.list_mistake_memory_sources(lesson_id=lesson.lesson_id, limit=10_000))
        if not sources or any(_utc(item.added_at_utc) > _utc(request.analysis_cutoff_utc) for item in sources):
            raise ValueError("Exact-case lesson provenance is missing or after the future cutoff.")
        resolution = self.store.get_degree_resolution(request.source_degree_resolution_id)
        if resolution is None:
            raise KeyError("Future source degree resolution is not registered.")
        orchestration = self.technical_orchestrator.orchestrate(
            request,
            existing_degree_resolution=resolution,
            shadow_mode=True,
            lesson_context_forecast_id=forecast.forecast_id,
            executed_at_utc=executed_at_utc,
        )
        self.store.create_forecast_agent_orchestration(orchestration)
        if orchestration.primary_result.input_packet_hash != technical_counter_input_hash(request, TechnicalAgentRole.PRIMARY_WAVE_COUNTER):
            raise ValueError("Primary counter input was not the independent decision-time packet.")
        if orchestration.alternative_result.input_packet_hash != technical_counter_input_hash(request, TechnicalAgentRole.ALTERNATIVE_WAVE_COUNTER):
            raise ValueError("Alternative counter input was not the independent decision-time packet.")
        trace = orchestration.stage_trace
        if not (trace.index("primary_frozen") < trace.index("alternative_frozen") < trace.index("candidates_validated") < trace.index("lessons_retrieved") < trace.index("auditor_frozen")):
            raise ValueError("Future analysis violated count-freeze and lesson-retrieval ordering.")
        retrieved = tuple(item for item in orchestration.retrieved_lessons if item.lesson_id == lesson.lesson_id)
        if not retrieved:
            raise ValueError("The expected cutoff-valid exact-case lesson was not retrieved.")
        retrieval_ids = {item.retrieval_id for item in retrieved}
        if any(not item.source_ids or not item.source_provenance or not item.audit_only for item in retrieved):
            raise ValueError("Retrieved lesson lacks complete audit-only provenance.")
        matching_audits = tuple(item for item in orchestration.evidence_audits if set(item.lesson_retrieval_ids) & retrieval_ids)
        if not matching_audits or any(not item.structural_validity_unchanged or lesson.content_hash not in item.lesson_content_hashes for item in matching_audits):
            raise ValueError("Auditor did not preserve validity while citing lesson provenance.")
        if not any(item.structurally_valid for item in orchestration.validation_results):
            raise ValueError("No structurally valid candidate survived the future proof fixture.")
        timestamp = _normalize_utc(executed_at_utc, field_name="executed_at_utc")
        lineage = _checkpoint(
            previous.lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.FUTURE_ANALYSIS_PROOF,
            input_hashes={"active_exact_case_lesson": lesson.content_hash, "phase5_ranking": before_hash, "future_technical_request": request.content_hash},
            output_ids={"future_technical_orchestration": orchestration.orchestration_id},
            output_hashes={"future_technical_orchestration": orchestration.content_hash},
            human_action_required=False,
            completed_at_utc=timestamp,
        )
        lineage = _checkpoint(
            lineage,
            workflow_id=previous.workflow_id,
            root_request_id=previous.lineage.root_request_id,
            root_request_hash=previous.lineage.root_request_hash,
            stage=ShadowWorkflowStage.COMPLETED,
            input_hashes={"future_technical_orchestration": orchestration.content_hash},
            output_ids={"active_exact_case_lesson": lesson.lesson_id},
            output_hashes={"active_exact_case_lesson": lesson.content_hash},
            human_action_required=False,
            completed_at_utc=timestamp,
        )
        return _result_from(
            previous,
            workflow_id=previous.workflow_id,
            status=ShadowWorkflowStatus.COMPLETED,
            current_stage=ShadowWorkflowStage.COMPLETED,
            lineage=lineage,
            updated_at_utc=timestamp,
            future_technical_orchestration_id=orchestration.orchestration_id,
            future_technical_orchestration_hash=orchestration.content_hash,
            human_gate=None,
        )


__all__ = [
    "HUMAN_ACTION_GATE_SCHEMA_VERSION",
    "OPERATOR_APPROVAL_SCHEMA_VERSION",
    "SHADOW_FORECAST_PROPOSAL_SCHEMA_VERSION",
    "SHADOW_WORKFLOW_CALCULATION_VERSION",
    "SHADOW_WORKFLOW_CHECKPOINT_SCHEMA_VERSION",
    "SHADOW_WORKFLOW_LINEAGE_SCHEMA_VERSION",
    "SHADOW_WORKFLOW_POLICY_VERSION",
    "SHADOW_WORKFLOW_SCHEMA_VERSION",
    "HumanActionGate",
    "OperatorApproval",
    "Phase11ShadowWorkflow",
    "ShadowForecastProposal",
    "ShadowWorkflowCheckpoint",
    "ShadowWorkflowLineage",
    "ShadowWorkflowResult",
    "ShadowWorkflowStage",
    "ShadowWorkflowStatus",
    "replay_shadow_workflow_result",
    "validate_shadow_workflow_result",
]
