"""Phase 11D2 outcome-learning agents in disabled-by-default shadow mode.

The agents in this module may draft a human outcome review or a mistake-memory
lesson proposal from already immutable Phase 11 records. They cannot create a
human review, lesson, lesson event, forecast, or active-pipeline decision.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .forecast_outcomes import (
    EvaluationPolicy,
    ForecastObservationSet,
    ForecastOutcomeEvaluation,
    OutcomeStatus,
    evaluation_policy_content_hash,
    forecast_observation_set_content_hash,
    forecast_outcome_evaluation_content_hash,
    validate_forecast_observation_set,
    validate_forecast_outcome_evaluation,
)
from .forecast_records import (
    ForecastRecord,
    canonical_sha256,
    forecast_record_content_hash,
    validate_forecast_record,
)
from .mistake_memory import (
    FailureDiagnosisType,
    ForecastOutcomeReview,
    LessonEvent,
    LessonScope,
    LessonScopeType,
    LessonSource,
    LessonStatus,
    MistakeMemoryLesson,
    ReviewDecision,
    forecast_outcome_review_content_hash,
    lesson_current_status,
    lesson_deduplication_key,
    lesson_event_content_hash,
    lesson_source_content_hash,
    mistake_memory_lesson_content_hash,
    validate_forecast_outcome_review,
    validate_lesson_event_chain,
)
from .providers import AnalysisProvider, ProviderError


OUTCOME_LEARNING_POLICY_VERSION = "phase11d2-shadow-policy-1.0.0"
OUTCOME_LEARNING_CALCULATION_VERSION = "phase11d2-deterministic-1.0.0"
OUTCOME_LEARNING_ROLE_SCHEMA_VERSION = "outcome-learning-agent-role-1.0.0"
OUTCOME_REVIEWER_REQUEST_SCHEMA_VERSION = "outcome-reviewer-request-1.0.0"
OUTCOME_REVIEW_DRAFT_SCHEMA_VERSION = "outcome-review-draft-1.0.0"
DIAGNOSIS_CANDIDATE_SCHEMA_VERSION = "diagnosis-candidate-1.0.0"
MISTAKE_PROPOSAL_REQUEST_SCHEMA_VERSION = "mistake-proposal-request-1.0.0"
MISTAKE_LESSON_DRAFT_SCHEMA_VERSION = "mistake-lesson-proposal-draft-1.0.0"
DUPLICATE_LESSON_CANDIDATE_SCHEMA_VERSION = "duplicate-lesson-candidate-1.0.0"
OUTCOME_LEARNING_RESULT_SCHEMA_VERSION = "outcome-learning-agent-result-1.0.0"
OUTCOME_LEARNING_ORCHESTRATION_SCHEMA_VERSION = (
    "outcome-learning-orchestration-1.0.0"
)

OUTCOME_REVIEWER_PROMPT_VERSION = "phase11d2-outcome-reviewer-1.0.0"
MISTAKE_PROPOSAL_PROMPT_VERSION = "phase11d2-mistake-proposal-1.0.0"


class OutcomeLearningAgentRole(StrEnum):
    OUTCOME_REVIEWER = "outcome_reviewer"
    MISTAKE_MEMORY_PROPOSAL = "mistake_memory_proposal"


class OutcomeLearningAgentResultStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class OutcomeLearningOrchestrationStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


OUTCOME_LEARNING_PROMPT_VERSIONS = MappingProxyType(
    {
        OutcomeLearningAgentRole.OUTCOME_REVIEWER.value: (
            OUTCOME_REVIEWER_PROMPT_VERSION
        ),
        OutcomeLearningAgentRole.MISTAKE_MEMORY_PROPOSAL.value: (
            MISTAKE_PROPOSAL_PROMPT_VERSION
        ),
    }
)

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "company_knowledge",
        "company_fundamentals",
        "fundamentals",
        "fundamental_analysis",
        "news",
        "market_news",
        "macro_news",
        "market_scenario",
        "scenario_narratives",
        "hidden_reasoning",
        "chain_of_thought",
        "unapproved_lesson_proposals",
        "live_market_file",
        "mutable_market_file",
    }
)
_NON_FAILURE_OUTCOMES = frozenset(
    {
        OutcomeStatus.UNRESOLVED,
        OutcomeStatus.INSUFFICIENT_DATA,
        OutcomeStatus.INCOMPARABLE,
    }
)
_LESSON_SUPPORT_OUTCOMES = frozenset(
    {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}
)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Canonical JSON cannot contain non-finite numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}.")


def canonical_outcome_learning_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Immutable JSON cannot contain non-finite numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported immutable JSON value: {type(value).__name__}.")


def _content_hash(value: Any) -> str:
    payload = value.to_dict() if isinstance(value, _JsonContract) else _json_value(value)
    if isinstance(payload, dict):
        payload.pop("content_hash", None)
    return canonical_sha256(payload)


def _normalize_utc(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name=field_name)


def _require_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _string_tuple(
    value: Sequence[str], *, field_name: str, sort_unique: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    normalized = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} cannot contain duplicates.")
    return tuple(sorted(normalized)) if sort_unique else normalized


def _string_mapping(value: Mapping[str, str], *, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    return MappingProxyType(
        {
            _require_text(key, field_name=f"{field_name}.key"): _require_text(
                item, field_name=f"{field_name}[{key!r}]"
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    )


def _enum_value(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid.") from exc


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], *, model_name: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{model_name} contains unknown fields: " + ", ".join(unknown) + "."
        )


def _find_forbidden_keys(value: Any, path: str = "input") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if str(key).strip().casefold() in _FORBIDDEN_INPUT_KEYS:
                findings.append(child_path)
            findings.extend(_find_forbidden_keys(item, child_path))
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            findings.extend(_find_forbidden_keys(item, f"{path}[{index}]"))
    return tuple(findings)


def _model_tuple(
    value: Sequence[Any], model_type: type[Any], *, field_name: str
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result: list[Any] = []
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
        return _json_value(self)


def _evaluation_reference_ids(
    forecast: ForecastRecord,
    observation: ForecastObservationSet,
    evaluation: ForecastOutcomeEvaluation,
) -> frozenset[str]:
    result = {
        forecast.forecast_id,
        observation.observation_set_id,
        evaluation.evaluation_id,
        observation.evaluation_policy.policy_id,
        *(item.dataset_id for item in forecast.dataset_cutoffs),
        *(item.window_id for item in forecast.expected_completion_windows),
        *(item.evidence_id for item in forecast.evidence),
        *(item.candle_id for item in observation.candles),
    }
    for hypothesis in (
        forecast.main_hypothesis,
        *forecast.alternative_hypotheses,
    ):
        result.add(hypothesis.hypothesis_id)
        result.update(hypothesis.target_claim_ids)
        result.update(hypothesis.invalidation_claim_ids)
        result.update(hypothesis.confirmation_claim_ids)
        result.update(hypothesis.completion_window_ids)
    for hypothesis in (
        evaluation.main_hypothesis_outcome,
        *evaluation.alternative_hypothesis_outcomes,
    ):
        result.add(hypothesis.hypothesis_id)
        result.update(hypothesis.supporting_candle_ids)
        for claim in hypothesis.claim_outcomes:
            result.add(claim.claim_id)
            result.update(claim.supporting_candle_ids)
    return frozenset(result)


def _forecast_hypothesis_dimensions(
    forecast: ForecastRecord,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    hypotheses = (forecast.main_hypothesis, *forecast.alternative_hypotheses)
    return (
        frozenset(item.hypothesis_id for item in hypotheses),
        frozenset(item.wave_label for item in hypotheses),
        frozenset(item.degree for item in hypotheses),
    )


@dataclass(frozen=True, slots=True)
class DiagnosisCandidate(_JsonContract):
    diagnosis_id: str
    diagnosis_type: FailureDiagnosisType
    summary: str
    affected_hypothesis_ids: tuple[str, ...]
    affected_claim_ids: tuple[str, ...]
    affected_wave_labels: tuple[str, ...]
    affected_degrees: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    contradictory_evidence_ids: tuple[str, ...]
    alternative_explanation: str | None
    price_first: bool = True
    human_confirmation_required: bool = True
    schema_version: str = DIAGNOSIS_CANDIDATE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnosis_id", _require_text(self.diagnosis_id, field_name="diagnosis_id"))
        object.__setattr__(self, "diagnosis_type", _enum_value(self.diagnosis_type, FailureDiagnosisType, field_name="diagnosis_type"))
        object.__setattr__(self, "summary", _require_text(self.summary, field_name="summary"))
        for name in (
            "affected_hypothesis_ids",
            "affected_claim_ids",
            "affected_wave_labels",
            "affected_degrees",
            "supporting_evidence_ids",
            "contradictory_evidence_ids",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=True))
        if set(self.supporting_evidence_ids) & set(self.contradictory_evidence_ids):
            raise ValueError("Diagnosis evidence cannot be both supporting and contradictory.")
        object.__setattr__(self, "alternative_explanation", _optional_text(self.alternative_explanation, field_name="alternative_explanation"))
        if not self.price_first or not self.human_confirmation_required:
            raise ValueError("Diagnosis candidates must remain price-first and human-confirmed.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "DiagnosisCandidate":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("diagnosis_id"):
            values["diagnosis_id"] = "diagnosis_candidate_" + canonical_sha256(
                {
                    "diagnosis_type": _json_value(values.get("diagnosis_type")),
                    "summary": values.get("summary"),
                    "affected_hypothesis_ids": values.get("affected_hypothesis_ids", ()),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "DiagnosisCandidate":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("DiagnosisCandidate content_hash does not match its payload.")
        return result


@dataclass(frozen=True, slots=True)
class DuplicateLessonCandidate(_JsonContract):
    duplicate_candidate_id: str
    lesson_id: str
    lesson_content_hash: str
    deduplication_key_match: bool
    scope_overlap: tuple[str, ...]
    match_reasons: tuple[str, ...]
    advisory_only: bool = True
    schema_version: str = DUPLICATE_LESSON_CANDIDATE_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("duplicate_candidate_id", "lesson_id", "schema_version"):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        object.__setattr__(self, "lesson_content_hash", _require_hash(self.lesson_content_hash, field_name="lesson_content_hash"))
        if not isinstance(self.deduplication_key_match, bool):
            raise TypeError("deduplication_key_match must be boolean.")
        object.__setattr__(self, "scope_overlap", _string_tuple(self.scope_overlap, field_name="scope_overlap", sort_unique=True))
        object.__setattr__(self, "match_reasons", _string_tuple(self.match_reasons, field_name="match_reasons"))
        if not self.match_reasons:
            raise ValueError("A duplicate candidate requires at least one match reason.")
        if not self.advisory_only:
            raise ValueError("Duplicate candidates are advisory and cannot merge lessons.")
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "DuplicateLessonCandidate":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("duplicate_candidate_id"):
            values["duplicate_candidate_id"] = "duplicate_lesson_candidate_" + canonical_sha256(
                {"lesson_id": values.get("lesson_id"), "match_reasons": values.get("match_reasons", ())}
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "DuplicateLessonCandidate":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("DuplicateLessonCandidate content_hash does not match its payload.")
        return result


def _validate_outcome_bundle(
    forecast: ForecastRecord,
    observation: ForecastObservationSet,
    evaluation: ForecastOutcomeEvaluation,
    policy: EvaluationPolicy,
) -> tuple[str, ...]:
    errors: list[str] = []
    errors.extend(validate_forecast_record(forecast))
    errors.extend(validate_forecast_observation_set(observation, forecast))
    if evaluation.lower_timeframe_observation_set_ids:
        errors.append(
            "Outcome Reviewer accepts one observation set; evaluations with lower-timeframe observation dependencies are unavailable in Phase 11D2."
        )
    else:
        errors.extend(
            validate_forecast_outcome_evaluation(
                evaluation,
                forecast=forecast,
                observation_set=observation,
            )
        )
    if policy.content_hash != evaluation_policy_content_hash(policy):
        errors.append("Evaluation policy hash does not match its payload.")
    if policy.policy_id != observation.evaluation_policy.policy_id:
        errors.append("Supplied evaluation policy ID differs from the observation set.")
    if policy.content_hash != observation.policy_hash:
        errors.append("Supplied evaluation policy hash differs from the observation set.")
    if evaluation.evaluation_policy_hash != policy.content_hash:
        errors.append("Outcome evaluation references a different policy hash.")
    if _utc(evaluation.evaluated_through_utc) > _utc(observation.horizon_end_utc):
        errors.append("Outcome evaluation extends beyond its predetermined horizon.")
    return tuple(errors)


@dataclass(frozen=True, slots=True)
class OutcomeReviewerRequest(_JsonContract):
    request_id: str
    forecast: ForecastRecord
    observation_set: ForecastObservationSet
    evaluation: ForecastOutcomeEvaluation
    evaluation_policy: EvaluationPolicy
    verified_evidence_ids: tuple[str, ...]
    frozen_input_hash: str
    source_hashes: Mapping[str, str]
    shadow_mode: bool
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    created_at_utc: str
    schema_version: str = OUTCOME_REVIEWER_REQUEST_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text(self.request_id, field_name="request_id"))
        for name, model_type in (
            ("forecast", ForecastRecord),
            ("observation_set", ForecastObservationSet),
            ("evaluation", ForecastOutcomeEvaluation),
            ("evaluation_policy", EvaluationPolicy),
        ):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, model_type.from_dict(value))
            elif not isinstance(value, model_type):
                raise TypeError(f"{name} must be a {model_type.__name__}.")
        errors = _validate_outcome_bundle(
            self.forecast,
            self.observation_set,
            self.evaluation,
            self.evaluation_policy,
        )
        if errors:
            raise ValueError("Invalid Outcome Reviewer inputs: " + " ".join(errors))
        forbidden = _find_forbidden_keys(self.forecast.to_dict(), "forecast")
        if forbidden:
            raise ValueError(
                "Outcome Reviewer input contains forbidden non-technical context: "
                + ", ".join(forbidden)
                + "."
            )
        evidence_ids = _string_tuple(
            self.verified_evidence_ids,
            field_name="verified_evidence_ids",
            sort_unique=True,
        )
        expected_evidence = _evaluation_reference_ids(
            self.forecast, self.observation_set, self.evaluation
        )
        if set(evidence_ids) != set(expected_evidence):
            raise ValueError(
                "verified_evidence_ids must exactly match immutable in-horizon references."
            )
        object.__setattr__(self, "verified_evidence_ids", evidence_ids)
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        expected_hashes = {
            "forecast_record": self.forecast.content_hash,
            "observation_set": self.observation_set.content_hash,
            "outcome_evaluation": self.evaluation.content_hash,
            "evaluation_policy": self.evaluation_policy.content_hash,
        }
        if dict(source_hashes) != expected_hashes:
            raise ValueError("Outcome Reviewer source_hashes do not match immutable inputs.")
        object.__setattr__(self, "source_hashes", source_hashes)
        if not isinstance(self.shadow_mode, bool):
            raise TypeError("shadow_mode must be boolean.")
        object.__setattr__(self, "provider", _require_text(self.provider, field_name="provider"))
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, field_name="prompt_version"))
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, field_name="policy_version"))
        object.__setattr__(self, "created_at_utc", _normalize_utc(self.created_at_utc, field_name="created_at_utc"))
        if _utc(self.created_at_utc) < _utc(self.evaluation.evaluated_through_utc):
            raise ValueError("Outcome Reviewer request cannot predate its evaluated horizon.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        expected_input_hash = canonical_sha256(
            {
                "forecast_hash": self.forecast.content_hash,
                "observation_set_hash": self.observation_set.content_hash,
                "evaluation_hash": self.evaluation.content_hash,
                "evaluation_policy_hash": self.evaluation_policy.content_hash,
                "verified_evidence_ids": self.verified_evidence_ids,
            }
        )
        if self.frozen_input_hash:
            object.__setattr__(self, "frozen_input_hash", _require_hash(self.frozen_input_hash, field_name="frozen_input_hash"))
            if self.frozen_input_hash != expected_input_hash:
                raise ValueError("Outcome Reviewer frozen_input_hash does not match.")
        else:
            object.__setattr__(self, "frozen_input_hash", expected_input_hash)
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "OutcomeReviewerRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("frozen_input_hash", "")
        if not values.get("request_id"):
            values["request_id"] = "outcome_reviewer_request_" + canonical_sha256(
                {
                    "forecast_id": getattr(values.get("forecast"), "forecast_id", None),
                    "evaluation_id": getattr(values.get("evaluation"), "evaluation_id", None),
                    "created_at_utc": values.get("created_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "OutcomeReviewerRequest":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("OutcomeReviewerRequest content_hash does not match its payload.")
        return result


def build_outcome_reviewer_request(
    forecast: ForecastRecord,
    observation_set: ForecastObservationSet,
    evaluation: ForecastOutcomeEvaluation,
    *,
    provider: str,
    model: str | None,
    created_at_utc: str,
    shadow_mode: bool = False,
) -> OutcomeReviewerRequest:
    return OutcomeReviewerRequest.create(
        request_id="",
        forecast=forecast,
        observation_set=observation_set,
        evaluation=evaluation,
        evaluation_policy=observation_set.evaluation_policy,
        verified_evidence_ids=tuple(
            sorted(_evaluation_reference_ids(forecast, observation_set, evaluation))
        ),
        source_hashes={
            "forecast_record": forecast.content_hash,
            "observation_set": observation_set.content_hash,
            "outcome_evaluation": evaluation.content_hash,
            "evaluation_policy": observation_set.evaluation_policy.content_hash,
        },
        shadow_mode=shadow_mode,
        provider=provider,
        model=model,
        prompt_version=OUTCOME_REVIEWER_PROMPT_VERSION,
        policy_version=OUTCOME_LEARNING_POLICY_VERSION,
        created_at_utc=created_at_utc,
    )


@dataclass(frozen=True, slots=True)
class OutcomeReviewDraft(_JsonContract):
    draft_id: str
    request_id: str
    request_content_hash: str
    forecast_id: str
    forecast_content_hash: str
    observation_set_id: str
    observation_set_content_hash: str
    evaluation_id: str
    evaluation_content_hash: str
    reviewed_hypothesis_id: str
    deterministic_outcome_status: OutcomeStatus
    deterministic_price_status: OutcomeStatus
    deterministic_timing_status: OutcomeStatus
    recommended_human_review_decision: ReviewDecision
    diagnoses: tuple[DiagnosisCandidate, ...]
    price_outcome_summary: str
    timing_outcome_summary: str
    main_hypothesis_summary: str
    alternative_hypothesis_summaries: Mapping[str, str]
    supporting_evidence_ids: tuple[str, ...]
    contradictory_evidence_ids: tuple[str, ...]
    alternative_explanations: tuple[str, ...]
    unavailable_warnings: tuple[str, ...]
    incomparable_warnings: tuple[str, ...]
    concise_reasoning_summary: tuple[str, ...]
    human_action_required: bool
    advisory_only: bool
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    generated_at_utc: str
    schema_version: str = OUTCOME_REVIEW_DRAFT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "draft_id", "request_id", "forecast_id", "observation_set_id",
            "evaluation_id", "reviewed_hypothesis_id", "price_outcome_summary",
            "timing_outcome_summary", "main_hypothesis_summary", "provider",
            "prompt_version", "policy_version", "schema_version",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), field_name=name))
        for name in (
            "request_content_hash", "forecast_content_hash",
            "observation_set_content_hash", "evaluation_content_hash",
        ):
            object.__setattr__(self, name, _require_hash(getattr(self, name), field_name=name))
        for name in (
            "deterministic_outcome_status",
            "deterministic_price_status",
            "deterministic_timing_status",
        ):
            object.__setattr__(self, name, _enum_value(getattr(self, name), OutcomeStatus, field_name=name))
        object.__setattr__(self, "recommended_human_review_decision", _enum_value(self.recommended_human_review_decision, ReviewDecision, field_name="recommended_human_review_decision"))
        object.__setattr__(self, "diagnoses", _model_tuple(self.diagnoses, DiagnosisCandidate, field_name="diagnoses"))
        if len({item.diagnosis_id for item in self.diagnoses}) != len(self.diagnoses):
            raise ValueError("Outcome review draft diagnoses must be unique.")
        object.__setattr__(self, "alternative_hypothesis_summaries", _string_mapping(self.alternative_hypothesis_summaries, field_name="alternative_hypothesis_summaries"))
        for name in (
            "supporting_evidence_ids", "contradictory_evidence_ids",
            "alternative_explanations", "unavailable_warnings",
            "incomparable_warnings", "concise_reasoning_summary",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), field_name=name, sort_unique=name in {"supporting_evidence_ids", "contradictory_evidence_ids"}))
        if set(self.supporting_evidence_ids) & set(self.contradictory_evidence_ids):
            raise ValueError("Draft evidence cannot be both supporting and contradictory.")
        if not self.concise_reasoning_summary:
            raise ValueError("Outcome review draft requires a concise summary.")
        if not self.human_action_required or not self.advisory_only:
            raise ValueError("Outcome review drafts must remain advisory and require human action.")
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(self, "generated_at_utc", _normalize_utc(self.generated_at_utc, field_name="generated_at_utc"))
        if self.content_hash:
            object.__setattr__(self, "content_hash", _require_hash(self.content_hash, field_name="content_hash"))

    @classmethod
    def create(cls, **values: Any) -> "OutcomeReviewDraft":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("draft_id"):
            values["draft_id"] = "outcome_review_draft_" + canonical_sha256(
                {
                    "request_content_hash": values.get("request_content_hash"),
                    "reviewed_hypothesis_id": values.get("reviewed_hypothesis_id"),
                    "generated_at_utc": values.get("generated_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "OutcomeReviewDraft":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError("OutcomeReviewDraft content_hash does not match its payload.")
        return result


def _validate_review_bundle(
    forecast: ForecastRecord,
    evaluation: ForecastOutcomeEvaluation,
    review: ForecastOutcomeReview,
    *,
    supporting: bool,
) -> tuple[str, ...]:
    errors: list[str] = []
    errors.extend(validate_forecast_record(forecast))
    if evaluation.content_hash != forecast_outcome_evaluation_content_hash(evaluation):
        errors.append("Outcome-evaluation content_hash does not match its payload.")
    if evaluation.forecast_id != forecast.forecast_id:
        errors.append("Outcome evaluation references a different forecast ID.")
    if evaluation.forecast_content_hash != forecast.content_hash:
        errors.append("Outcome evaluation references a different forecast hash.")
    review_errors = validate_forecast_outcome_review(review, evaluation=evaluation)
    # A persisted revision's parent is verified by Phase 11C persistence. The
    # D2 request intentionally carries only the selected immutable review.
    errors.extend(
        item for item in review_errors if "initial review" not in item.casefold()
    )
    if review.decision not in {ReviewDecision.APPROVED, ReviewDecision.REVISED}:
        errors.append(
            "Mistake Memory Proposal requires an explicitly approved or revised human review."
        )
    if review.deterministic_scoring_error:
        errors.append(
            "A review declaring deterministic scoring error cannot support a lesson proposal."
        )
    if supporting and review.reviewed_outcome_status not in _LESSON_SUPPORT_OUTCOMES:
        errors.append(
            "Supporting reviews must preserve a deterministic failed or partial outcome."
        )
    return tuple(errors)


def _normalize_model_sequence(
    value: Sequence[Any], model_type: type[Any], *, field_name: str
) -> tuple[Any, ...]:
    return _model_tuple(value, model_type, field_name=field_name)


def _bundle_maps(
    forecasts: Sequence[ForecastRecord],
    evaluations: Sequence[ForecastOutcomeEvaluation],
    reviews: Sequence[ForecastOutcomeReview],
) -> tuple[
    dict[str, ForecastRecord],
    dict[str, ForecastOutcomeEvaluation],
    dict[str, ForecastOutcomeReview],
]:
    forecast_map = {item.forecast_id: item for item in forecasts}
    evaluation_map = {item.evaluation_id: item for item in evaluations}
    review_map = {item.review_id: item for item in reviews}
    if len(forecast_map) != len(forecasts):
        raise ValueError("Forecast bundles cannot contain duplicate forecast IDs.")
    if len(evaluation_map) != len(evaluations):
        raise ValueError("Forecast bundles cannot contain duplicate evaluation IDs.")
    if len(review_map) != len(reviews):
        raise ValueError("Forecast bundles cannot contain duplicate review IDs.")
    return forecast_map, evaluation_map, review_map


@dataclass(frozen=True, slots=True)
class MistakeMemoryProposalRequest(_JsonContract):
    request_id: str
    forecast: ForecastRecord
    evaluation: ForecastOutcomeEvaluation
    approved_review: ForecastOutcomeReview
    additional_supporting_forecasts: tuple[ForecastRecord, ...]
    additional_supporting_evaluations: tuple[ForecastOutcomeEvaluation, ...]
    additional_supporting_reviews: tuple[ForecastOutcomeReview, ...]
    counterexample_forecasts: tuple[ForecastRecord, ...]
    counterexample_evaluations: tuple[ForecastOutcomeEvaluation, ...]
    counterexample_reviews: tuple[ForecastOutcomeReview, ...]
    existing_lessons: tuple[MistakeMemoryLesson, ...]
    existing_lesson_sources: tuple[LessonSource, ...]
    existing_lesson_events: tuple[LessonEvent, ...]
    existing_lesson_reference_id: str | None
    verified_review_ids: tuple[str, ...]
    verified_lesson_ids: tuple[str, ...]
    verified_lesson_source_ids: tuple[str, ...]
    frozen_input_hash: str
    source_hashes: Mapping[str, str]
    shadow_mode: bool
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    created_at_utc: str
    schema_version: str = MISTAKE_PROPOSAL_REQUEST_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_text(self.request_id, field_name="request_id")
        )
        for name, model_type in (
            ("forecast", ForecastRecord),
            ("evaluation", ForecastOutcomeEvaluation),
            ("approved_review", ForecastOutcomeReview),
        ):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, model_type.from_dict(value))
            elif not isinstance(value, model_type):
                raise TypeError(f"{name} must be a {model_type.__name__}.")
        for name, model_type in (
            ("additional_supporting_forecasts", ForecastRecord),
            ("additional_supporting_evaluations", ForecastOutcomeEvaluation),
            ("additional_supporting_reviews", ForecastOutcomeReview),
            ("counterexample_forecasts", ForecastRecord),
            ("counterexample_evaluations", ForecastOutcomeEvaluation),
            ("counterexample_reviews", ForecastOutcomeReview),
            ("existing_lessons", MistakeMemoryLesson),
            ("existing_lesson_sources", LessonSource),
            ("existing_lesson_events", LessonEvent),
        ):
            object.__setattr__(
                self,
                name,
                _normalize_model_sequence(
                    getattr(self, name), model_type, field_name=name
                ),
            )
        object.__setattr__(
            self,
            "existing_lesson_reference_id",
            _optional_text(
                self.existing_lesson_reference_id,
                field_name="existing_lesson_reference_id",
            ),
        )
        for name in (
            "verified_review_ids",
            "verified_lesson_ids",
            "verified_lesson_source_ids",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name, sort_unique=True),
            )
        if not isinstance(self.shadow_mode, bool):
            raise TypeError("shadow_mode must be boolean.")
        object.__setattr__(
            self, "provider", _require_text(self.provider, field_name="provider")
        )
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(
            self,
            "prompt_version",
            _require_text(self.prompt_version, field_name="prompt_version"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _require_text(self.policy_version, field_name="policy_version"),
        )
        object.__setattr__(
            self,
            "created_at_utc",
            _normalize_utc(self.created_at_utc, field_name="created_at_utc"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, field_name="schema_version"),
        )

        supporting_forecasts = (
            self.forecast,
            *self.additional_supporting_forecasts,
        )
        supporting_evaluations = (
            self.evaluation,
            *self.additional_supporting_evaluations,
        )
        supporting_reviews = (
            self.approved_review,
            *self.additional_supporting_reviews,
        )
        support_forecast_map, support_evaluation_map, support_review_map = _bundle_maps(
            supporting_forecasts, supporting_evaluations, supporting_reviews
        )
        counter_forecast_map, counter_evaluation_map, counter_review_map = _bundle_maps(
            self.counterexample_forecasts,
            self.counterexample_evaluations,
            self.counterexample_reviews,
        )
        if set(support_review_map) & set(counter_review_map):
            raise ValueError("A review cannot be both supporting and a counterexample.")
        if set(support_evaluation_map) & set(counter_evaluation_map):
            raise ValueError("An evaluation cannot be in both proposal evidence groups.")

        validation_errors: list[str] = []
        for review in supporting_reviews:
            evaluation = support_evaluation_map.get(review.evaluation_id)
            forecast = support_forecast_map.get(review.forecast_id)
            if evaluation is None or forecast is None:
                validation_errors.append(
                    f"Supporting review {review.review_id} lacks its forecast or evaluation."
                )
                continue
            validation_errors.extend(
                _validate_review_bundle(forecast, evaluation, review, supporting=True)
            )
        for review in self.counterexample_reviews:
            evaluation = counter_evaluation_map.get(review.evaluation_id)
            forecast = counter_forecast_map.get(review.forecast_id)
            if evaluation is None or forecast is None:
                validation_errors.append(
                    f"Counterexample review {review.review_id} lacks its forecast or evaluation."
                )
                continue
            validation_errors.extend(
                _validate_review_bundle(forecast, evaluation, review, supporting=False)
            )
        if validation_errors:
            raise ValueError(
                "Invalid Mistake Memory Proposal inputs: "
                + " ".join(validation_errors)
            )
        if _utc(self.created_at_utc) < max(
            _utc(item.reviewed_at_utc)
            for item in (*supporting_reviews, *self.counterexample_reviews)
        ):
            raise ValueError("Mistake proposal request cannot predate a supplied review.")

        all_reviews = (*supporting_reviews, *self.counterexample_reviews)
        if set(self.verified_review_ids) != {item.review_id for item in all_reviews}:
            raise ValueError("verified_review_ids must exactly match supplied reviews.")

        lessons_by_id = {item.lesson_id: item for item in self.existing_lessons}
        if len(lessons_by_id) != len(self.existing_lessons):
            raise ValueError("Existing lessons cannot contain duplicate IDs.")
        if set(self.verified_lesson_ids) != set(lessons_by_id):
            raise ValueError("verified_lesson_ids must exactly match supplied lessons.")
        if (
            self.existing_lesson_reference_id is not None
            and self.existing_lesson_reference_id not in lessons_by_id
        ):
            raise ValueError("existing_lesson_reference_id is not a supplied active lesson.")

        sources_by_id = {item.source_id: item for item in self.existing_lesson_sources}
        if len(sources_by_id) != len(self.existing_lesson_sources):
            raise ValueError("Existing lesson sources cannot contain duplicate IDs.")
        if set(self.verified_lesson_source_ids) != set(sources_by_id):
            raise ValueError(
                "verified_lesson_source_ids must exactly match supplied lesson sources."
            )
        for lesson in self.existing_lessons:
            if lesson.content_hash != mistake_memory_lesson_content_hash(lesson):
                raise ValueError(f"Existing lesson {lesson.lesson_id} has an invalid hash.")
            if _utc(lesson.proposed_at_utc) > _utc(self.created_at_utc):
                raise ValueError(
                    f"Existing lesson {lesson.lesson_id} was proposed after the request cutoff."
                )
            events = tuple(
                item for item in self.existing_lesson_events if item.lesson_id == lesson.lesson_id
            )
            event_errors = validate_lesson_event_chain(lesson, events)
            if event_errors:
                raise ValueError(
                    f"Existing lesson {lesson.lesson_id} has an invalid event chain: "
                    + " ".join(event_errors)
                )
            if (
                lesson_current_status(
                    lesson, events, at_or_before_utc=self.created_at_utc
                )
                is not LessonStatus.ACTIVE
            ):
                raise ValueError(
                    f"Existing lesson {lesson.lesson_id} was not active at request cutoff."
                )
        for source in self.existing_lesson_sources:
            lesson = lessons_by_id.get(source.lesson_id)
            if lesson is None:
                raise ValueError(
                    f"Lesson source {source.source_id} references an unsupplied lesson."
                )
            if source.content_hash != lesson_source_content_hash(source):
                raise ValueError(f"Lesson source {source.source_id} has an invalid hash.")
            if source.lesson_content_hash != lesson.content_hash:
                raise ValueError(
                    f"Lesson source {source.source_id} references a different lesson hash."
                )
            if _utc(source.added_at_utc) > _utc(self.created_at_utc):
                raise ValueError(
                    f"Lesson source {source.source_id} was added after the request cutoff."
                )
        known_event_ids: set[str] = set()
        for event in self.existing_lesson_events:
            if event.event_id in known_event_ids:
                raise ValueError("Existing lesson events cannot contain duplicate IDs.")
            known_event_ids.add(event.event_id)
            if event.content_hash != lesson_event_content_hash(event):
                raise ValueError(f"Lesson event {event.event_id} has an invalid hash.")
            if event.lesson_id not in lessons_by_id:
                raise ValueError(
                    f"Lesson event {event.event_id} references an unsupplied lesson."
                )
            if _utc(event.recorded_at_utc) > _utc(self.created_at_utc):
                raise ValueError(
                    f"Lesson event {event.event_id} is after the request cutoff."
                )

        expected_hashes: dict[str, str] = {}
        for item in (*supporting_forecasts, *self.counterexample_forecasts):
            expected_hashes[f"forecast:{item.forecast_id}"] = item.content_hash
        for item in (*supporting_evaluations, *self.counterexample_evaluations):
            expected_hashes[f"evaluation:{item.evaluation_id}"] = item.content_hash
        for item in all_reviews:
            expected_hashes[f"review:{item.review_id}"] = item.content_hash
        for item in self.existing_lessons:
            expected_hashes[f"lesson:{item.lesson_id}"] = item.content_hash
        for item in self.existing_lesson_sources:
            expected_hashes[f"lesson_source:{item.source_id}"] = item.content_hash
        for item in self.existing_lesson_events:
            expected_hashes[f"lesson_event:{item.event_id}"] = item.content_hash
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        if dict(source_hashes) != expected_hashes:
            raise ValueError("Mistake proposal source_hashes do not match immutable inputs.")
        object.__setattr__(self, "source_hashes", source_hashes)
        expected_input_hash = canonical_sha256(
            {
                "source_hashes": expected_hashes,
                "supporting_review_ids": sorted(support_review_map),
                "counterexample_review_ids": sorted(counter_review_map),
                "existing_lesson_reference_id": self.existing_lesson_reference_id,
                "policy_version": self.policy_version,
            }
        )
        if self.frozen_input_hash:
            object.__setattr__(
                self,
                "frozen_input_hash",
                _require_hash(self.frozen_input_hash, field_name="frozen_input_hash"),
            )
            if self.frozen_input_hash != expected_input_hash:
                raise ValueError("Mistake proposal frozen_input_hash does not match.")
        else:
            object.__setattr__(self, "frozen_input_hash", expected_input_hash)
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "MistakeMemoryProposalRequest":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        values.setdefault("frozen_input_hash", "")
        if not values.get("request_id"):
            review = values.get("approved_review")
            values["request_id"] = "mistake_proposal_request_" + canonical_sha256(
                {
                    "review_id": getattr(review, "review_id", None),
                    "created_at_utc": values.get("created_at_utc"),
                    "existing_lesson_reference_id": values.get(
                        "existing_lesson_reference_id"
                    ),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "MistakeMemoryProposalRequest":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError(
                "MistakeMemoryProposalRequest content_hash does not match its payload."
            )
        return result


def build_mistake_memory_proposal_request(
    forecast: ForecastRecord,
    evaluation: ForecastOutcomeEvaluation,
    approved_review: ForecastOutcomeReview,
    *,
    provider: str,
    model: str | None,
    created_at_utc: str,
    additional_supporting_bundles: Sequence[
        tuple[ForecastRecord, ForecastOutcomeEvaluation, ForecastOutcomeReview]
    ] = (),
    counterexample_bundles: Sequence[
        tuple[ForecastRecord, ForecastOutcomeEvaluation, ForecastOutcomeReview]
    ] = (),
    existing_lessons: Sequence[MistakeMemoryLesson] = (),
    existing_lesson_sources: Sequence[LessonSource] = (),
    existing_lesson_events: Sequence[LessonEvent] = (),
    existing_lesson_reference_id: str | None = None,
    shadow_mode: bool = False,
) -> MistakeMemoryProposalRequest:
    supporting_forecasts = tuple(item[0] for item in additional_supporting_bundles)
    supporting_evaluations = tuple(item[1] for item in additional_supporting_bundles)
    supporting_reviews = tuple(item[2] for item in additional_supporting_bundles)
    counter_forecasts = tuple(item[0] for item in counterexample_bundles)
    counter_evaluations = tuple(item[1] for item in counterexample_bundles)
    counter_reviews = tuple(item[2] for item in counterexample_bundles)
    all_forecasts = (forecast, *supporting_forecasts, *counter_forecasts)
    all_evaluations = (evaluation, *supporting_evaluations, *counter_evaluations)
    all_reviews = (approved_review, *supporting_reviews, *counter_reviews)
    hashes: dict[str, str] = {
        **{f"forecast:{item.forecast_id}": item.content_hash for item in all_forecasts},
        **{
            f"evaluation:{item.evaluation_id}": item.content_hash
            for item in all_evaluations
        },
        **{f"review:{item.review_id}": item.content_hash for item in all_reviews},
        **{f"lesson:{item.lesson_id}": item.content_hash for item in existing_lessons},
        **{
            f"lesson_source:{item.source_id}": item.content_hash
            for item in existing_lesson_sources
        },
        **{
            f"lesson_event:{item.event_id}": item.content_hash
            for item in existing_lesson_events
        },
    }
    return MistakeMemoryProposalRequest.create(
        request_id="",
        forecast=forecast,
        evaluation=evaluation,
        approved_review=approved_review,
        additional_supporting_forecasts=supporting_forecasts,
        additional_supporting_evaluations=supporting_evaluations,
        additional_supporting_reviews=supporting_reviews,
        counterexample_forecasts=counter_forecasts,
        counterexample_evaluations=counter_evaluations,
        counterexample_reviews=counter_reviews,
        existing_lessons=tuple(existing_lessons),
        existing_lesson_sources=tuple(existing_lesson_sources),
        existing_lesson_events=tuple(existing_lesson_events),
        existing_lesson_reference_id=existing_lesson_reference_id,
        verified_review_ids=tuple(sorted(item.review_id for item in all_reviews)),
        verified_lesson_ids=tuple(sorted(item.lesson_id for item in existing_lessons)),
        verified_lesson_source_ids=tuple(
            sorted(item.source_id for item in existing_lesson_sources)
        ),
        frozen_input_hash="",
        source_hashes=hashes,
        shadow_mode=shadow_mode,
        provider=provider,
        model=model,
        prompt_version=MISTAKE_PROPOSAL_PROMPT_VERSION,
        policy_version=OUTCOME_LEARNING_POLICY_VERSION,
        created_at_utc=created_at_utc,
    )


@dataclass(frozen=True, slots=True)
class MistakeLessonProposalDraft(_JsonContract):
    draft_id: str
    request_id: str
    request_content_hash: str
    forecast_id: str
    evaluation_id: str
    approved_review_id: str
    recommended_scope: LessonScope
    title: str
    warning_text: str
    recommended_audit_check: str
    supporting_review_ids: tuple[str, ...]
    counterexample_review_ids: tuple[str, ...]
    possible_duplicate_lesson_ids: tuple[str, ...]
    duplicate_candidates: tuple[DuplicateLessonCandidate, ...]
    existing_lesson_reference_id: str | None
    proposal_rationale: tuple[str, ...]
    limitations: tuple[str, ...]
    concise_reasoning_summary: tuple[str, ...]
    human_action_required: bool
    advisory_only: bool
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    generated_at_utc: str
    schema_version: str = MISTAKE_LESSON_DRAFT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "draft_id",
            "request_id",
            "forecast_id",
            "evaluation_id",
            "approved_review_id",
            "title",
            "warning_text",
            "recommended_audit_check",
            "provider",
            "prompt_version",
            "policy_version",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "request_content_hash",
            _require_hash(self.request_content_hash, field_name="request_content_hash"),
        )
        if isinstance(self.recommended_scope, Mapping):
            object.__setattr__(
                self,
                "recommended_scope",
                LessonScope.from_dict(self.recommended_scope),
            )
        elif not isinstance(self.recommended_scope, LessonScope):
            raise TypeError("recommended_scope must be a LessonScope.")
        for name in (
            "supporting_review_ids",
            "counterexample_review_ids",
            "possible_duplicate_lesson_ids",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name, sort_unique=True),
            )
        if not self.supporting_review_ids:
            raise ValueError("A lesson proposal draft requires supporting reviews.")
        if set(self.supporting_review_ids) & set(self.counterexample_review_ids):
            raise ValueError("A review cannot support and contradict the same draft.")
        object.__setattr__(
            self,
            "duplicate_candidates",
            _model_tuple(
                self.duplicate_candidates,
                DuplicateLessonCandidate,
                field_name="duplicate_candidates",
            ),
        )
        if {item.lesson_id for item in self.duplicate_candidates} != set(
            self.possible_duplicate_lesson_ids
        ):
            raise ValueError(
                "Duplicate candidates must exactly match possible_duplicate_lesson_ids."
            )
        object.__setattr__(
            self,
            "existing_lesson_reference_id",
            _optional_text(
                self.existing_lesson_reference_id,
                field_name="existing_lesson_reference_id",
            ),
        )
        for name in (
            "proposal_rationale",
            "limitations",
            "concise_reasoning_summary",
        ):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name), field_name=name)
            )
        if not self.proposal_rationale or not self.concise_reasoning_summary:
            raise ValueError("A lesson proposal requires rationale and a concise summary.")
        if not self.human_action_required or not self.advisory_only:
            raise ValueError("Lesson proposal drafts are advisory and require human action.")
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(
            self,
            "generated_at_utc",
            _normalize_utc(self.generated_at_utc, field_name="generated_at_utc"),
        )
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "MistakeLessonProposalDraft":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("draft_id"):
            values["draft_id"] = "mistake_lesson_draft_" + canonical_sha256(
                {
                    "request_content_hash": values.get("request_content_hash"),
                    "supporting_review_ids": values.get("supporting_review_ids", ()),
                    "generated_at_utc": values.get("generated_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "MistakeLessonProposalDraft":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError(
                "MistakeLessonProposalDraft content_hash does not match its payload."
            )
        return result


@dataclass(frozen=True, slots=True)
class OutcomeLearningAgentResult(_JsonContract):
    result_id: str
    role: OutcomeLearningAgentRole
    status: OutcomeLearningAgentResultStatus
    request_id: str
    request_content_hash: str
    frozen_input_hash: str
    input_packet_hash: str
    prompt_hash: str
    structured_output_hash: str | None
    draft_id: str | None
    draft_content_hash: str | None
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    retry_count: int
    human_action_required: bool
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    started_at_utc: str
    completed_at_utc: str
    schema_version: str = OUTCOME_LEARNING_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "result_id",
            "request_id",
            "provider",
            "prompt_version",
            "policy_version",
            "schema_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "role",
            _enum_value(self.role, OutcomeLearningAgentRole, field_name="role"),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status, OutcomeLearningAgentResultStatus, field_name="status"
            ),
        )
        for name in (
            "request_content_hash",
            "frozen_input_hash",
            "input_packet_hash",
            "prompt_hash",
        ):
            object.__setattr__(
                self, name, _require_hash(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "structured_output_hash",
            None
            if self.structured_output_hash is None
            else _require_hash(
                self.structured_output_hash, field_name="structured_output_hash"
            ),
        )
        object.__setattr__(
            self, "draft_id", _optional_text(self.draft_id, field_name="draft_id")
        )
        object.__setattr__(
            self,
            "draft_content_hash",
            None
            if self.draft_content_hash is None
            else _require_hash(self.draft_content_hash, field_name="draft_content_hash"),
        )
        if bool(self.draft_id) != bool(self.draft_content_hash):
            raise ValueError("draft_id and draft_content_hash must be present together.")
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count < 0
        ):
            raise ValueError("retry_count must be a non-negative integer.")
        if not isinstance(self.human_action_required, bool):
            raise TypeError("human_action_required must be boolean.")
        object.__setattr__(
            self, "warnings", _string_tuple(self.warnings, field_name="warnings")
        )
        object.__setattr__(self, "errors", _string_tuple(self.errors, field_name="errors"))
        object.__setattr__(
            self,
            "started_at_utc",
            _normalize_utc(self.started_at_utc, field_name="started_at_utc"),
        )
        object.__setattr__(
            self,
            "completed_at_utc",
            _normalize_utc(self.completed_at_utc, field_name="completed_at_utc"),
        )
        if _utc(self.completed_at_utc) < _utc(self.started_at_utc):
            raise ValueError("An outcome-learning result cannot complete before it starts.")
        if self.status is OutcomeLearningAgentResultStatus.FAILED:
            if not self.errors:
                raise ValueError("A failed outcome-learning result must explain its failure.")
            if self.draft_id or self.draft_content_hash:
                raise ValueError("A failed result cannot retain an accepted draft reference.")
        else:
            if self.errors:
                raise ValueError("A successful outcome-learning result cannot contain errors.")
            if not self.draft_id or not self.draft_content_hash:
                raise ValueError("A successful outcome-learning result requires a draft.")
            if not self.human_action_required:
                raise ValueError("Every successful agent output requires human action.")
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @classmethod
    def create(cls, **values: Any) -> "OutcomeLearningAgentResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("result_id"):
            values["result_id"] = "outcome_learning_result_" + canonical_sha256(
                {
                    "role": _json_value(values.get("role")),
                    "request_content_hash": values.get("request_content_hash"),
                    "structured_output_hash": values.get("structured_output_hash"),
                    "draft_content_hash": values.get("draft_content_hash"),
                    "completed_at_utc": values.get("completed_at_utc"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "OutcomeLearningAgentResult":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError(
                "OutcomeLearningAgentResult content_hash does not match its payload."
            )
        return result


def outcome_learning_agent_result_content_hash(
    value: OutcomeLearningAgentResult | Mapping[str, Any],
) -> str:
    return _content_hash(value)


@dataclass(frozen=True, slots=True)
class OutcomeLearningOrchestration(_JsonContract):
    orchestration_id: str
    orchestration_version: int
    supersedes_orchestration_id: str | None
    orchestration_kind: OutcomeLearningAgentRole
    request: OutcomeReviewerRequest | MistakeMemoryProposalRequest
    result: OutcomeLearningAgentResult
    outcome_review_draft: OutcomeReviewDraft | None
    mistake_lesson_proposal_draft: MistakeLessonProposalDraft | None
    status: OutcomeLearningOrchestrationStatus
    human_action_required: bool
    source_hashes: Mapping[str, str]
    warnings: tuple[str, ...]
    validation_errors: tuple[str, ...]
    provider: str
    model: str | None
    prompt_version: str
    policy_version: str
    started_at_utc: str
    completed_at_utc: str
    schema_version: str = OUTCOME_LEARNING_ORCHESTRATION_SCHEMA_VERSION
    calculation_version: str = OUTCOME_LEARNING_CALCULATION_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "orchestration_id",
            "provider",
            "prompt_version",
            "policy_version",
            "schema_version",
            "calculation_version",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), field_name=name)
            )
        if (
            not isinstance(self.orchestration_version, int)
            or isinstance(self.orchestration_version, bool)
            or self.orchestration_version < 1
        ):
            raise ValueError("orchestration_version must be a positive integer.")
        object.__setattr__(
            self,
            "supersedes_orchestration_id",
            _optional_text(
                self.supersedes_orchestration_id,
                field_name="supersedes_orchestration_id",
            ),
        )
        if self.supersedes_orchestration_id == self.orchestration_id:
            raise ValueError("An orchestration cannot supersede itself.")
        object.__setattr__(
            self,
            "orchestration_kind",
            _enum_value(
                self.orchestration_kind,
                OutcomeLearningAgentRole,
                field_name="orchestration_kind",
            ),
        )
        request_type = (
            OutcomeReviewerRequest
            if self.orchestration_kind is OutcomeLearningAgentRole.OUTCOME_REVIEWER
            else MistakeMemoryProposalRequest
        )
        if isinstance(self.request, Mapping):
            object.__setattr__(self, "request", request_type.from_dict(self.request))
        elif not isinstance(self.request, request_type):
            raise TypeError(
                f"{self.orchestration_kind.value} requires {request_type.__name__}."
            )
        if isinstance(self.result, Mapping):
            object.__setattr__(
                self, "result", OutcomeLearningAgentResult.from_dict(self.result)
            )
        elif not isinstance(self.result, OutcomeLearningAgentResult):
            raise TypeError("result must be an OutcomeLearningAgentResult.")
        if isinstance(self.outcome_review_draft, Mapping):
            object.__setattr__(
                self,
                "outcome_review_draft",
                OutcomeReviewDraft.from_dict(self.outcome_review_draft),
            )
        elif self.outcome_review_draft is not None and not isinstance(
            self.outcome_review_draft, OutcomeReviewDraft
        ):
            raise TypeError("outcome_review_draft has an invalid type.")
        if isinstance(self.mistake_lesson_proposal_draft, Mapping):
            object.__setattr__(
                self,
                "mistake_lesson_proposal_draft",
                MistakeLessonProposalDraft.from_dict(
                    self.mistake_lesson_proposal_draft
                ),
            )
        elif self.mistake_lesson_proposal_draft is not None and not isinstance(
            self.mistake_lesson_proposal_draft, MistakeLessonProposalDraft
        ):
            raise TypeError("mistake_lesson_proposal_draft has an invalid type.")
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status, OutcomeLearningOrchestrationStatus, field_name="status"
            ),
        )
        if not isinstance(self.human_action_required, bool):
            raise TypeError("human_action_required must be boolean.")
        source_hashes = _string_mapping(self.source_hashes, field_name="source_hashes")
        for key, item in source_hashes.items():
            _require_hash(item, field_name=f"source_hashes[{key!r}]")
        object.__setattr__(self, "source_hashes", source_hashes)
        object.__setattr__(
            self, "warnings", _string_tuple(self.warnings, field_name="warnings")
        )
        object.__setattr__(
            self,
            "validation_errors",
            _string_tuple(self.validation_errors, field_name="validation_errors"),
        )
        object.__setattr__(self, "model", _optional_text(self.model, field_name="model"))
        object.__setattr__(
            self,
            "started_at_utc",
            _normalize_utc(self.started_at_utc, field_name="started_at_utc"),
        )
        object.__setattr__(
            self,
            "completed_at_utc",
            _normalize_utc(self.completed_at_utc, field_name="completed_at_utc"),
        )
        if _utc(self.completed_at_utc) < _utc(self.started_at_utc):
            raise ValueError("An orchestration cannot complete before it starts.")

        if self.result.role is not self.orchestration_kind:
            raise ValueError("Agent result role differs from orchestration kind.")
        if self.result.request_id != self.request.request_id:
            raise ValueError("Agent result references a different request ID.")
        if self.result.request_content_hash != self.request.content_hash:
            raise ValueError("Agent result references a different request hash.")
        if self.result.frozen_input_hash != self.request.frozen_input_hash:
            raise ValueError("Agent result references a different frozen input hash.")
        if self.result.provider != self.provider or self.request.provider != self.provider:
            raise ValueError("Provider identity differs across orchestration records.")
        if self.result.model != self.model or self.request.model != self.model:
            raise ValueError("Model identity differs across orchestration records.")
        if (
            self.result.prompt_version != self.prompt_version
            or self.request.prompt_version != self.prompt_version
        ):
            raise ValueError("Prompt version differs across orchestration records.")
        if (
            self.result.policy_version != self.policy_version
            or self.request.policy_version != self.policy_version
        ):
            raise ValueError("Policy version differs across orchestration records.")
        if self.result.started_at_utc != self.started_at_utc:
            raise ValueError("Result and orchestration start timestamps differ.")
        if self.result.completed_at_utc != self.completed_at_utc:
            raise ValueError("Result and orchestration completion timestamps differ.")

        draft: OutcomeReviewDraft | MistakeLessonProposalDraft | None
        if self.orchestration_kind is OutcomeLearningAgentRole.OUTCOME_REVIEWER:
            if self.mistake_lesson_proposal_draft is not None:
                raise ValueError("Outcome Reviewer cannot contain a lesson proposal draft.")
            draft = self.outcome_review_draft
        else:
            if self.outcome_review_draft is not None:
                raise ValueError("Mistake Memory Proposal cannot contain an outcome draft.")
            draft = self.mistake_lesson_proposal_draft
        if draft is None:
            if self.status is not OutcomeLearningOrchestrationStatus.FAILED:
                raise ValueError("A successful orchestration requires its structured draft.")
            if self.result.status is not OutcomeLearningAgentResultStatus.FAILED:
                raise ValueError("A draft-free orchestration must retain a failed result.")
        else:
            if self.status is OutcomeLearningOrchestrationStatus.FAILED:
                raise ValueError("A failed orchestration cannot expose a structured draft.")
            if draft.request_id != self.request.request_id:
                raise ValueError("Structured draft references a different request ID.")
            if draft.request_content_hash != self.request.content_hash:
                raise ValueError("Structured draft references a different request hash.")
            if self.result.draft_id != draft.draft_id:
                raise ValueError("Agent result references a different draft ID.")
            if self.result.draft_content_hash != draft.content_hash:
                raise ValueError("Agent result references a different draft hash.")
            if not self.human_action_required or not self.result.human_action_required:
                raise ValueError("Every successful orchestration requires human action.")
        expected_source_hashes = {
            "request": self.request.content_hash,
            "agent_result": self.result.content_hash,
        }
        if draft is not None:
            expected_source_hashes["structured_draft"] = draft.content_hash
        if dict(self.source_hashes) != expected_source_hashes:
            raise ValueError("Orchestration source_hashes do not match its records.")
        expected_status = OutcomeLearningOrchestrationStatus(self.result.status.value)
        if self.status is not expected_status:
            raise ValueError("Orchestration status differs from its agent result.")
        if self.status is OutcomeLearningOrchestrationStatus.FAILED:
            if tuple(self.validation_errors) != tuple(self.result.errors):
                raise ValueError("Failed orchestration errors must mirror its result.")
        elif self.validation_errors:
            raise ValueError("A successful orchestration cannot retain validation errors.")
        if self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                _require_hash(self.content_hash, field_name="content_hash"),
            )

    @property
    def forecast_id(self) -> str:
        return self.request.forecast.forecast_id

    @property
    def observation_set_id(self) -> str | None:
        if isinstance(self.request, OutcomeReviewerRequest):
            return self.request.observation_set.observation_set_id
        return None

    @property
    def evaluation_id(self) -> str:
        return self.request.evaluation.evaluation_id

    @property
    def outcome_review_id(self) -> str | None:
        if isinstance(self.request, MistakeMemoryProposalRequest):
            return self.request.approved_review.review_id
        return None

    @property
    def existing_lesson_id(self) -> str | None:
        if isinstance(self.request, MistakeMemoryProposalRequest):
            return self.request.existing_lesson_reference_id
        return None

    @property
    def structured_draft_hash(self) -> str | None:
        return self.result.draft_content_hash

    @classmethod
    def create(cls, **values: Any) -> "OutcomeLearningOrchestration":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("orchestration_id"):
            values["orchestration_id"] = "outcome_learning_orchestration_" + canonical_sha256(
                {
                    "kind": _json_value(values.get("orchestration_kind")),
                    "request_hash": getattr(values.get("request"), "content_hash", None),
                    "result_hash": getattr(values.get("result"), "content_hash", None),
                    "version": values.get("orchestration_version"),
                    "supersedes": values.get("supersedes_orchestration_id"),
                }
            )[:32]
        result = cls(**values)
        return replace(result, content_hash=_content_hash(result))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "OutcomeLearningOrchestration":
        raw = dict(value)
        _reject_unknown(raw, {item.name for item in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != _content_hash(result):
            raise ValueError(
                "OutcomeLearningOrchestration content_hash does not match its payload."
            )
        return result


def outcome_learning_orchestration_content_hash(
    value: OutcomeLearningOrchestration | Mapping[str, Any],
) -> str:
    return _content_hash(value)


def validate_outcome_learning_orchestration(
    orchestration: OutcomeLearningOrchestration,
) -> tuple[str, ...]:
    errors: list[str] = []
    if orchestration.schema_version != OUTCOME_LEARNING_ORCHESTRATION_SCHEMA_VERSION:
        errors.append("Unsupported outcome-learning orchestration schema_version.")
    if orchestration.calculation_version != OUTCOME_LEARNING_CALCULATION_VERSION:
        errors.append("Unsupported outcome-learning calculation_version.")
    if orchestration.policy_version != OUTCOME_LEARNING_POLICY_VERSION:
        errors.append("Unsupported outcome-learning policy_version.")
    if orchestration.content_hash != outcome_learning_orchestration_content_hash(
        orchestration
    ):
        errors.append("Outcome-learning orchestration content_hash does not match.")
    if orchestration.result.content_hash != outcome_learning_agent_result_content_hash(
        orchestration.result
    ):
        errors.append("Outcome-learning agent-result hash does not match.")
    if isinstance(orchestration.request, OutcomeReviewerRequest):
        if orchestration.request.content_hash != _content_hash(orchestration.request):
            errors.append("Outcome Reviewer request hash does not match.")
        if orchestration.outcome_review_draft is not None and (
            orchestration.outcome_review_draft.content_hash
            != _content_hash(orchestration.outcome_review_draft)
        ):
            errors.append("Outcome review draft hash does not match.")
    else:
        if orchestration.request.content_hash != _content_hash(orchestration.request):
            errors.append("Mistake proposal request hash does not match.")
        if orchestration.mistake_lesson_proposal_draft is not None and (
            orchestration.mistake_lesson_proposal_draft.content_hash
            != _content_hash(orchestration.mistake_lesson_proposal_draft)
        ):
            errors.append("Mistake lesson proposal draft hash does not match.")
    return tuple(errors)


def replay_outcome_learning_orchestration(
    value: OutcomeLearningOrchestration | Mapping[str, Any],
    *,
    expected_content_hash: str | None = None,
) -> OutcomeLearningOrchestration:
    """Replay immutable D2 contracts and hashes without another provider call."""

    result = (
        value
        if isinstance(value, OutcomeLearningOrchestration)
        else OutcomeLearningOrchestration.from_dict(value)
    )
    errors = validate_outcome_learning_orchestration(result)
    if errors:
        raise ValueError("Outcome-learning replay failed: " + " ".join(errors))
    if expected_content_hash is not None and result.content_hash != expected_content_hash:
        raise ValueError(
            "Outcome-learning replay content hash differs from the expected hash."
        )
    return result


def _strict_schema_errors(
    value: Any, schema: Mapping[str, Any], path: str = "output"
) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    types = tuple(expected_type) if isinstance(expected_type, list) else (expected_type,)

    def matches(name: Any) -> bool:
        if name == "object":
            return isinstance(value, Mapping)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        if name == "boolean":
            return isinstance(value, bool)
        if name == "null":
            return value is None
        return name is None

    if types != (None,) and not any(matches(name) for name in types):
        errors.append(f"{path} has the wrong JSON type.")
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} is outside its controlled enum.")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for required in schema.get("required", ()):
            if required not in value:
                errors.append(f"{path} is missing {required}.")
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path} contains unknown field {key!r}.")
        for key, child in value.items():
            if key in properties:
                errors.extend(
                    _strict_schema_errors(child, properties[key], f"{path}.{key}")
                )
    elif isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            errors.extend(
                _strict_schema_errors(child, schema["items"], f"{path}[{index}]")
            )
    return errors


_STRING_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
}

_DIAGNOSIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "diagnosis_type",
        "summary",
        "affected_hypothesis_ids",
        "affected_claim_ids",
        "affected_wave_labels",
        "affected_degrees",
        "supporting_evidence_ids",
        "contradictory_evidence_ids",
        "alternative_explanation",
    ],
    "properties": {
        "diagnosis_type": {
            "type": "string",
            "enum": [item.value for item in FailureDiagnosisType],
        },
        "summary": {"type": "string"},
        "affected_hypothesis_ids": _STRING_ARRAY_SCHEMA,
        "affected_claim_ids": _STRING_ARRAY_SCHEMA,
        "affected_wave_labels": _STRING_ARRAY_SCHEMA,
        "affected_degrees": _STRING_ARRAY_SCHEMA,
        "supporting_evidence_ids": _STRING_ARRAY_SCHEMA,
        "contradictory_evidence_ids": _STRING_ARRAY_SCHEMA,
        "alternative_explanation": {"type": ["string", "null"]},
    },
}

OUTCOME_REVIEWER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reviewed_hypothesis_id",
        "recommended_human_review_decision",
        "diagnoses",
        "price_outcome_summary",
        "timing_outcome_summary",
        "main_hypothesis_summary",
        "alternative_hypothesis_summaries",
        "supporting_evidence_ids",
        "contradictory_evidence_ids",
        "alternative_explanations",
        "unavailable_warnings",
        "incomparable_warnings",
        "concise_reasoning_summary",
    ],
    "properties": {
        "reviewed_hypothesis_id": {"type": "string"},
        "recommended_human_review_decision": {
            "type": "string",
            "enum": [item.value for item in ReviewDecision],
        },
        "diagnoses": {"type": "array", "items": _DIAGNOSIS_OUTPUT_SCHEMA},
        "price_outcome_summary": {"type": "string"},
        "timing_outcome_summary": {"type": "string"},
        "main_hypothesis_summary": {"type": "string"},
        "alternative_hypothesis_summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["hypothesis_id", "summary"],
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "supporting_evidence_ids": _STRING_ARRAY_SCHEMA,
        "contradictory_evidence_ids": _STRING_ARRAY_SCHEMA,
        "alternative_explanations": _STRING_ARRAY_SCHEMA,
        "unavailable_warnings": _STRING_ARRAY_SCHEMA,
        "incomparable_warnings": _STRING_ARRAY_SCHEMA,
        "concise_reasoning_summary": _STRING_ARRAY_SCHEMA,
    },
}

_LESSON_SCOPE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scope_type",
        "exact_forecast_ids",
        "symbols",
        "asset_classes",
        "timeframes",
        "degrees",
        "wave_roles",
        "pattern_families",
        "directions",
        "indicator_regimes",
        "market_regimes",
        "applicability_conditions",
        "non_applicability_conditions",
    ],
    "properties": {
        "scope_type": {
            "type": "string",
            "enum": [item.value for item in LessonScopeType],
        },
        "exact_forecast_ids": _STRING_ARRAY_SCHEMA,
        "symbols": _STRING_ARRAY_SCHEMA,
        "asset_classes": _STRING_ARRAY_SCHEMA,
        "timeframes": _STRING_ARRAY_SCHEMA,
        "degrees": _STRING_ARRAY_SCHEMA,
        "wave_roles": _STRING_ARRAY_SCHEMA,
        "pattern_families": _STRING_ARRAY_SCHEMA,
        "directions": _STRING_ARRAY_SCHEMA,
        "indicator_regimes": _STRING_ARRAY_SCHEMA,
        "market_regimes": _STRING_ARRAY_SCHEMA,
        "applicability_conditions": _STRING_ARRAY_SCHEMA,
        "non_applicability_conditions": _STRING_ARRAY_SCHEMA,
    },
}

MISTAKE_MEMORY_PROPOSAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "recommended_scope",
        "title",
        "warning_text",
        "recommended_audit_check",
        "supporting_review_ids",
        "counterexample_review_ids",
        "possible_duplicate_lesson_ids",
        "existing_lesson_reference_id",
        "proposal_rationale",
        "limitations",
        "concise_reasoning_summary",
    ],
    "properties": {
        "recommended_scope": _LESSON_SCOPE_OUTPUT_SCHEMA,
        "title": {"type": "string"},
        "warning_text": {"type": "string"},
        "recommended_audit_check": {"type": "string"},
        "supporting_review_ids": _STRING_ARRAY_SCHEMA,
        "counterexample_review_ids": _STRING_ARRAY_SCHEMA,
        "possible_duplicate_lesson_ids": _STRING_ARRAY_SCHEMA,
        "existing_lesson_reference_id": {"type": ["string", "null"]},
        "proposal_rationale": _STRING_ARRAY_SCHEMA,
        "limitations": _STRING_ARRAY_SCHEMA,
        "concise_reasoning_summary": _STRING_ARRAY_SCHEMA,
    },
}

_OUTCOME_REVIEWER_SYSTEM_PROMPT = """You are the Phase 11D2 Outcome Reviewer in disabled shadow mode.
Use only the supplied immutable forecast, post-cutoff observation set, deterministic outcome
evaluation, and verified IDs. Deterministic claim outcomes, price/timing statuses, event times,
and the original forecast are immutable. Diagnose price-first and preserve main and alternative
hypotheses separately. RSI, volume, EWO, MACD, and Fibonacci are soft evidence only. Never turn
unresolved, insufficient, or incomparable evidence into failure. Do not use news, fundamentals,
company knowledge, data outside the stated horizon, hidden reasoning, or mistake proposals. Draft
only advisory human-review material. Do not create or approve a ForecastOutcomeReview or lesson.
Return exactly one JSON object matching the schema with concise conclusions, never chain-of-thought."""

_MISTAKE_PROPOSAL_SYSTEM_PROMPT = """You are the Phase 11D2 Mistake Memory Proposal Agent in disabled shadow mode.
Use only supplied immutable forecasts, deterministic evaluations, explicitly approved human
reviews, and cutoff-valid active lessons. Draft the narrowest evidence-supported warning. One
forecast may support only exact_case scope; scoped requires at least two independent forecasts;
general requires at least three independent forecasts across two symbols. Do not use a human
exception, create a lesson or lifecycle event, activate or merge memory, override thresholds,
generalize automatically, or change Elliott rules, prompts, weights, confidence, probabilities,
Phase 5 ranking, or the live analysis. Reference only supplied review and lesson IDs. Return one
strict JSON object with concise conclusions and no hidden chain-of-thought."""


def _user_prompt(packet: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    return (
        "Return one JSON object matching this schema. Use only the frozen input packet.\n\n"
        "OUTPUT SCHEMA:\n"
        + json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n\nFROZEN INPUT:\n"
        + canonical_outcome_learning_json(packet)
    )


def _outcome_reviewer_packet(request: OutcomeReviewerRequest) -> dict[str, Any]:
    return {
        "agent_role": OutcomeLearningAgentRole.OUTCOME_REVIEWER.value,
        "request_reference": {
            "request_id": request.request_id,
            "request_content_hash": request.content_hash,
            "frozen_input_hash": request.frozen_input_hash,
            "created_at_utc": request.created_at_utc,
            "evaluation_horizon_end_utc": request.observation_set.horizon_end_utc,
        },
        "forecast": request.forecast.to_dict(),
        "observation_set": request.observation_set.to_dict(),
        "deterministic_evaluation": request.evaluation.to_dict(),
        "evaluation_policy": request.evaluation_policy.to_dict(),
        "verified_evidence_ids": list(request.verified_evidence_ids),
        "immutable_boundaries": {
            "deterministic_outcomes_are_read_only": True,
            "human_review_must_be_created_separately": True,
            "future_candles_beyond_horizon_forbidden": True,
            "indicators_are_soft_evidence": True,
        },
    }


def _mistake_proposal_packet(
    request: MistakeMemoryProposalRequest,
) -> dict[str, Any]:
    return {
        "agent_role": OutcomeLearningAgentRole.MISTAKE_MEMORY_PROPOSAL.value,
        "request_reference": {
            "request_id": request.request_id,
            "request_content_hash": request.content_hash,
            "frozen_input_hash": request.frozen_input_hash,
            "created_at_utc": request.created_at_utc,
        },
        "primary_bundle": {
            "forecast": request.forecast.to_dict(),
            "evaluation": request.evaluation.to_dict(),
            "approved_review": request.approved_review.to_dict(),
        },
        "additional_supporting_bundles": [
            {
                "forecast": forecast.to_dict(),
                "evaluation": evaluation.to_dict(),
                "approved_review": review.to_dict(),
            }
            for forecast, evaluation, review in zip(
                request.additional_supporting_forecasts,
                request.additional_supporting_evaluations,
                request.additional_supporting_reviews,
                strict=True,
            )
        ],
        "counterexample_bundles": [
            {
                "forecast": forecast.to_dict(),
                "evaluation": evaluation.to_dict(),
                "approved_review": review.to_dict(),
            }
            for forecast, evaluation, review in zip(
                request.counterexample_forecasts,
                request.counterexample_evaluations,
                request.counterexample_reviews,
                strict=True,
            )
        ],
        "existing_active_lessons": [item.to_dict() for item in request.existing_lessons],
        "existing_lesson_sources": [
            item.to_dict() for item in request.existing_lesson_sources
        ],
        "existing_lesson_events": [
            item.to_dict() for item in request.existing_lesson_events
        ],
        "existing_lesson_reference_id": request.existing_lesson_reference_id,
        "verified_review_ids": list(request.verified_review_ids),
        "verified_lesson_ids": list(request.verified_lesson_ids),
        "scope_policy": {
            "exact_case": "one supporting forecast",
            "scoped": "at least two independent supporting forecasts",
            "general": "at least three independent forecasts across at least two symbols",
            "human_exception_available_to_agent": False,
        },
        "immutable_boundaries": {
            "lesson_creation_requires_later_human_action": True,
            "activation_requires_later_human_event": True,
            "automatic_merge_forbidden": True,
            "automatic_generalization_forbidden": True,
        },
    }


@dataclass(frozen=True, slots=True)
class _ProviderCall:
    output: Mapping[str, Any] | None
    output_hash: str | None
    retry_count: int
    attempt_errors: tuple[str, ...]


def _provider_method(provider: AnalysisProvider) -> Callable[..., dict[str, Any]]:
    strict = getattr(provider, "generate_strict_json", None)
    return strict if callable(strict) else provider.generate


def _call_provider(
    provider: AnalysisProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    schema: Mapping[str, Any],
    packet: Mapping[str, Any],
    max_retries: int,
) -> _ProviderCall:
    method = _provider_method(provider)
    attempt_errors: list[str] = []
    last_output_hash: str | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = method(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=copy.deepcopy(dict(schema)),
                packet=copy.deepcopy(dict(packet)),
            )
            if not isinstance(raw, Mapping):
                raise ProviderError("Provider output must be a JSON object.")
            output = copy.deepcopy(dict(raw))
            last_output_hash = canonical_sha256(output)
            schema_errors = _strict_schema_errors(output, schema)
            if schema_errors:
                raise ProviderError(
                    "Strict structured-output validation failed: "
                    + " ".join(schema_errors)
                )
            return _ProviderCall(
                output=_freeze_json(output),
                output_hash=last_output_hash,
                retry_count=attempt,
                attempt_errors=tuple(attempt_errors),
            )
        except (ProviderError, TypeError, ValueError, KeyError) as exc:
            attempt_errors.append(f"attempt {attempt + 1}: {exc}")
    return _ProviderCall(
        output=None,
        output_hash=last_output_hash,
        retry_count=max_retries,
        attempt_errors=tuple(attempt_errors),
    )


def _hypothesis_outcome(
    evaluation: ForecastOutcomeEvaluation, hypothesis_id: str
) -> Any:
    for item in (
        evaluation.main_hypothesis_outcome,
        *evaluation.alternative_hypothesis_outcomes,
    ):
        if item.hypothesis_id == hypothesis_id:
            return item
    raise ValueError(f"Provider referenced unknown hypothesis {hypothesis_id!r}.")


def _parse_outcome_review_draft(
    request: OutcomeReviewerRequest,
    output: Mapping[str, Any],
    *,
    generated_at_utc: str,
) -> OutcomeReviewDraft:
    hypothesis_id = _require_text(
        output["reviewed_hypothesis_id"], field_name="reviewed_hypothesis_id"
    )
    selected = _hypothesis_outcome(request.evaluation, hypothesis_id)
    recommendation = _enum_value(
        output["recommended_human_review_decision"],
        ReviewDecision,
        field_name="recommended_human_review_decision",
    )
    if selected.outcome_status in _NON_FAILURE_OUTCOMES:
        if recommendation is not ReviewDecision.NEEDS_MORE_DATA:
            raise ValueError(
                "Unresolved, insufficient, or incomparable outcomes must remain needs_more_data."
            )
    elif selected.outcome_status is OutcomeStatus.SUCCEEDED:
        if recommendation is not ReviewDecision.APPROVED:
            raise ValueError("A succeeded deterministic outcome may only recommend approved.")

    verified = set(request.verified_evidence_ids)
    hypothesis_ids, wave_labels, degrees = _forecast_hypothesis_dimensions(
        request.forecast
    )
    claim_ids = {
        *(item.claim_id for item in request.forecast.target_claims),
        *(item.claim_id for item in request.forecast.invalidation_claims),
        *(item.claim_id for item in request.forecast.confirmation_claims),
    }
    diagnoses: list[DiagnosisCandidate] = []
    for index, raw in enumerate(output["diagnoses"]):
        diagnosis = DiagnosisCandidate.create(
            diagnosis_id="",
            diagnosis_type=raw["diagnosis_type"],
            summary=raw["summary"],
            affected_hypothesis_ids=raw["affected_hypothesis_ids"],
            affected_claim_ids=raw["affected_claim_ids"],
            affected_wave_labels=raw["affected_wave_labels"],
            affected_degrees=raw["affected_degrees"],
            supporting_evidence_ids=raw["supporting_evidence_ids"],
            contradictory_evidence_ids=raw["contradictory_evidence_ids"],
            alternative_explanation=raw["alternative_explanation"],
        )
        unknown = (
            set(diagnosis.affected_hypothesis_ids)
            | set(diagnosis.affected_claim_ids)
            | set(diagnosis.supporting_evidence_ids)
            | set(diagnosis.contradictory_evidence_ids)
        ) - verified
        if unknown:
            raise ValueError(
                f"Diagnosis {index + 1} contains invented references: "
                + ", ".join(sorted(unknown))
                + "."
            )
        if set(diagnosis.affected_hypothesis_ids) - set(hypothesis_ids):
            raise ValueError("Diagnosis references an unknown hypothesis.")
        if set(diagnosis.affected_claim_ids) - claim_ids:
            raise ValueError("Diagnosis references an unknown claim.")
        if set(diagnosis.affected_wave_labels) - set(wave_labels):
            raise ValueError("Diagnosis references an unknown wave label.")
        if set(diagnosis.affected_degrees) - set(degrees):
            raise ValueError("Diagnosis references an unknown degree.")
        if selected.outcome_status in _NON_FAILURE_OUTCOMES and diagnosis.diagnosis_type not in {
            FailureDiagnosisType.DATA_QUALITY_PROBLEM,
            FailureDiagnosisType.AMBIGUOUS_OR_INCOMPARABLE,
        }:
            raise ValueError(
                "Unresolved or incomparable outcomes may only retain data-quality or ambiguity diagnoses."
            )
        diagnoses.append(diagnosis)
    if selected.outcome_status is OutcomeStatus.SUCCEEDED and diagnoses:
        raise ValueError("A succeeded deterministic outcome cannot be recast as failure.")

    summaries: dict[str, str] = {}
    for item in output["alternative_hypothesis_summaries"]:
        item_id = _require_text(item["hypothesis_id"], field_name="hypothesis_id")
        if item_id in summaries:
            raise ValueError("Alternative hypothesis summaries contain duplicate IDs.")
        summaries[item_id] = _require_text(item["summary"], field_name="summary")
    expected_alternatives = {
        item.hypothesis_id for item in request.evaluation.alternative_hypothesis_outcomes
    }
    if set(summaries) != expected_alternatives:
        raise ValueError(
            "Alternative hypothesis summaries must preserve every frozen alternative separately."
        )
    supporting = _string_tuple(
        output["supporting_evidence_ids"],
        field_name="supporting_evidence_ids",
        sort_unique=True,
    )
    contradictory = _string_tuple(
        output["contradictory_evidence_ids"],
        field_name="contradictory_evidence_ids",
        sort_unique=True,
    )
    unknown = (set(supporting) | set(contradictory)) - verified
    if unknown:
        raise ValueError(
            "Outcome review draft contains invented evidence references: "
            + ", ".join(sorted(unknown))
            + "."
        )
    return OutcomeReviewDraft.create(
        draft_id="",
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        forecast_id=request.forecast.forecast_id,
        forecast_content_hash=request.forecast.content_hash,
        observation_set_id=request.observation_set.observation_set_id,
        observation_set_content_hash=request.observation_set.content_hash,
        evaluation_id=request.evaluation.evaluation_id,
        evaluation_content_hash=request.evaluation.content_hash,
        reviewed_hypothesis_id=hypothesis_id,
        deterministic_outcome_status=selected.outcome_status,
        deterministic_price_status=selected.price_status,
        deterministic_timing_status=selected.timing_status,
        recommended_human_review_decision=recommendation,
        diagnoses=tuple(diagnoses),
        price_outcome_summary=output["price_outcome_summary"],
        timing_outcome_summary=output["timing_outcome_summary"],
        main_hypothesis_summary=output["main_hypothesis_summary"],
        alternative_hypothesis_summaries=summaries,
        supporting_evidence_ids=supporting,
        contradictory_evidence_ids=contradictory,
        alternative_explanations=output["alternative_explanations"],
        unavailable_warnings=output["unavailable_warnings"],
        incomparable_warnings=output["incomparable_warnings"],
        concise_reasoning_summary=output["concise_reasoning_summary"],
        human_action_required=True,
        advisory_only=True,
        provider=request.provider,
        model=request.model,
        prompt_version=request.prompt_version,
        policy_version=request.policy_version,
        generated_at_utc=generated_at_utc,
    )


def _forecast_for_review(
    request: MistakeMemoryProposalRequest, review_id: str
) -> tuple[ForecastRecord, ForecastOutcomeEvaluation, ForecastOutcomeReview]:
    bundles = (
        (request.forecast, request.evaluation, request.approved_review),
        *zip(
            request.additional_supporting_forecasts,
            request.additional_supporting_evaluations,
            request.additional_supporting_reviews,
            strict=True,
        ),
        *zip(
            request.counterexample_forecasts,
            request.counterexample_evaluations,
            request.counterexample_reviews,
            strict=True,
        ),
    )
    for forecast, evaluation, review in bundles:
        if review.review_id == review_id:
            return forecast, evaluation, review
    raise ValueError(f"Unknown review reference {review_id!r}.")


def _reviewed_hypothesis(forecast: ForecastRecord, review: ForecastOutcomeReview) -> Any:
    for hypothesis in (forecast.main_hypothesis, *forecast.alternative_hypotheses):
        if hypothesis.hypothesis_id == review.reviewed_hypothesis_id:
            return hypothesis
    raise ValueError("Approved review references an unavailable forecast hypothesis.")


def _scope_overlap(left: LessonScope, right: LessonScope) -> tuple[str, ...]:
    overlaps: list[str] = []
    for name in (
        "exact_forecast_ids",
        "symbols",
        "asset_classes",
        "timeframes",
        "degrees",
        "wave_roles",
        "pattern_families",
        "directions",
        "indicator_regimes",
        "market_regimes",
    ):
        if set(getattr(left, name)) & set(getattr(right, name)):
            overlaps.append(name)
    if left.scope_type is right.scope_type:
        overlaps.append("scope_type")
    return tuple(sorted(overlaps))


def _validate_proposed_scope(
    request: MistakeMemoryProposalRequest,
    scope: LessonScope,
    supporting_review_ids: tuple[str, ...],
) -> None:
    bundles = [_forecast_for_review(request, item) for item in supporting_review_ids]
    distinct_forecasts = {item[0].forecast_id for item in bundles}
    distinct_symbols = {item[0].symbol.casefold() for item in bundles}
    if scope.scope_type is LessonScopeType.EXACT_CASE:
        if len(supporting_review_ids) != 1:
            raise ValueError("An exact-case draft must use exactly one supporting review.")
        forecast, _, review = bundles[0]
        hypothesis = _reviewed_hypothesis(forecast, review)
        required = {
            "exact_forecast_ids": (forecast.forecast_id,),
            "symbols": (forecast.symbol,),
            "timeframes": (forecast.dataset_cutoffs[0].timeframe,),
            "degrees": (hypothesis.degree,),
            "wave_roles": (hypothesis.wave_label,),
            "pattern_families": (hypothesis.pattern_family,),
            "directions": (hypothesis.direction.value,),
        }
        for name, expected in required.items():
            if tuple(getattr(scope, name)) != tuple(sorted(expected)):
                raise ValueError(
                    f"Exact-case scope {name} must match the reviewed forecast exactly."
                )
    elif scope.scope_type is LessonScopeType.SCOPED:
        if len(distinct_forecasts) < 2:
            raise ValueError(
                "A scoped lesson requires two independent supporting forecasts."
            )
    else:
        if len(distinct_forecasts) < 3 or len(distinct_symbols) < 2:
            raise ValueError(
                "A general lesson requires three independent forecasts across two symbols; "
                "the shadow agent cannot use a human exception."
            )


def _parse_mistake_lesson_draft(
    request: MistakeMemoryProposalRequest,
    output: Mapping[str, Any],
    *,
    generated_at_utc: str,
) -> MistakeLessonProposalDraft:
    scope = LessonScope.from_dict(output["recommended_scope"])
    supporting = _string_tuple(
        output["supporting_review_ids"],
        field_name="supporting_review_ids",
        sort_unique=True,
    )
    counterexamples = _string_tuple(
        output["counterexample_review_ids"],
        field_name="counterexample_review_ids",
        sort_unique=True,
    )
    support_available = {
        request.approved_review.review_id,
        *(item.review_id for item in request.additional_supporting_reviews),
    }
    counter_available = {item.review_id for item in request.counterexample_reviews}
    if not set(supporting).issubset(support_available):
        raise ValueError("Lesson proposal contains an invented supporting review ID.")
    if request.approved_review.review_id not in supporting:
        raise ValueError("The explicitly approved primary review must support the draft.")
    if not set(counterexamples).issubset(counter_available):
        raise ValueError("Lesson proposal contains an invented counterexample review ID.")
    _validate_proposed_scope(request, scope, supporting)

    existing_reference = _optional_text(
        output["existing_lesson_reference_id"],
        field_name="existing_lesson_reference_id",
    )
    if existing_reference != request.existing_lesson_reference_id:
        raise ValueError(
            "Provider cannot add, remove, or switch the approved existing-lesson reference."
        )
    possible_duplicates = _string_tuple(
        output["possible_duplicate_lesson_ids"],
        field_name="possible_duplicate_lesson_ids",
        sort_unique=True,
    )
    lessons = {item.lesson_id: item for item in request.existing_lessons}
    if not set(possible_duplicates).issubset(lessons):
        raise ValueError("Lesson proposal contains an invented duplicate lesson ID.")
    proposed_key = lesson_deduplication_key(
        scope=scope,
        warning_text=output["warning_text"],
        recommended_audit_check=output["recommended_audit_check"],
    )
    exact_duplicates = {
        item.lesson_id
        for item in request.existing_lessons
        if item.deduplication_key == proposed_key
    }
    if not exact_duplicates.issubset(set(possible_duplicates)):
        raise ValueError("All exact deterministic duplicate lessons must be disclosed.")
    duplicate_candidates: list[DuplicateLessonCandidate] = []
    for lesson_id in possible_duplicates:
        lesson = lessons[lesson_id]
        overlap = _scope_overlap(scope, lesson.scope)
        exact = lesson.deduplication_key == proposed_key
        if not exact and not overlap:
            raise ValueError(
                f"Possible duplicate {lesson_id} has no deterministic scope overlap."
            )
        reasons = (
            ("exact deterministic deduplication key",)
            if exact
            else tuple(f"scope overlap: {item}" for item in overlap)
        )
        duplicate_candidates.append(
            DuplicateLessonCandidate.create(
                duplicate_candidate_id="",
                lesson_id=lesson.lesson_id,
                lesson_content_hash=lesson.content_hash,
                deduplication_key_match=exact,
                scope_overlap=overlap,
                match_reasons=reasons,
                advisory_only=True,
            )
        )
    return MistakeLessonProposalDraft.create(
        draft_id="",
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        forecast_id=request.forecast.forecast_id,
        evaluation_id=request.evaluation.evaluation_id,
        approved_review_id=request.approved_review.review_id,
        recommended_scope=scope,
        title=output["title"],
        warning_text=output["warning_text"],
        recommended_audit_check=output["recommended_audit_check"],
        supporting_review_ids=supporting,
        counterexample_review_ids=counterexamples,
        possible_duplicate_lesson_ids=possible_duplicates,
        duplicate_candidates=tuple(duplicate_candidates),
        existing_lesson_reference_id=existing_reference,
        proposal_rationale=output["proposal_rationale"],
        limitations=output["limitations"],
        concise_reasoning_summary=output["concise_reasoning_summary"],
        human_action_required=True,
        advisory_only=True,
        provider=request.provider,
        model=request.model,
        prompt_version=request.prompt_version,
        policy_version=request.policy_version,
        generated_at_utc=generated_at_utc,
    )


def _prompt_hash(
    system_prompt: str,
    user_prompt: str,
    schema: Mapping[str, Any],
    prompt_version: str,
) -> str:
    return canonical_sha256(
        {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "schema": schema,
            "prompt_version": prompt_version,
        }
    )


class OutcomeLearningAgentOrchestrator:
    """Run either approved Phase 11D2 role without active-pipeline effects."""

    def __init__(
        self,
        *,
        provider: AnalysisProvider,
        max_retries: int = 0,
        stage_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not hasattr(provider, "generate"):
            raise TypeError("provider must implement AnalysisProvider.generate().")
        if (
            not isinstance(max_retries, int)
            or isinstance(max_retries, bool)
            or not 0 <= max_retries <= 3
        ):
            raise ValueError("max_retries must be between 0 and 3.")
        self.provider = provider
        self.max_retries = max_retries
        self.stage_callback = stage_callback

    def _stage(self, name: str) -> None:
        if self.stage_callback is not None:
            self.stage_callback(name)

    def _verify_shadow(
        self,
        request: OutcomeReviewerRequest | MistakeMemoryProposalRequest,
        shadow_mode: bool,
    ) -> None:
        if not request.shadow_mode or not shadow_mode:
            raise PermissionError(
                "Phase 11D2 is disabled by default; both request and invocation must set shadow_mode=True."
            )
        if request.provider != str(getattr(self.provider, "name", "")):
            raise ValueError("Request provider identity differs from runtime provider.")
        runtime_model = getattr(self.provider, "model", None)
        if request.model != runtime_model:
            raise ValueError("Request model identity differs from runtime provider model.")

    def _orchestration(
        self,
        *,
        request: OutcomeReviewerRequest | MistakeMemoryProposalRequest,
        role: OutcomeLearningAgentRole,
        packet: Mapping[str, Any],
        system_prompt: str,
        schema: Mapping[str, Any],
        parser: Callable[..., OutcomeReviewDraft | MistakeLessonProposalDraft],
        executed_at_utc: str,
        orchestration_version: int,
        supersedes_orchestration_id: str | None,
    ) -> OutcomeLearningOrchestration:
        timestamp = _normalize_utc(executed_at_utc, field_name="executed_at_utc")
        if _utc(timestamp) < _utc(request.created_at_utc):
            raise ValueError("D2 execution cannot predate its immutable request.")
        user_prompt = _user_prompt(packet, schema)
        prompt_hash = _prompt_hash(
            system_prompt, user_prompt, schema, request.prompt_version
        )
        input_packet_hash = canonical_sha256(packet)
        self._stage(f"{role.value}:provider_call")
        call = _call_provider(
            self.provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            packet=packet,
            max_retries=self.max_retries,
        )
        draft: OutcomeReviewDraft | MistakeLessonProposalDraft | None = None
        errors: tuple[str, ...] = ()
        warnings = call.attempt_errors if call.output is not None else ()
        if call.output is None:
            errors = call.attempt_errors or ("Provider returned no structured output.",)
        else:
            self._stage(f"{role.value}:reference_validation")
            try:
                draft = parser(request, call.output, generated_at_utc=timestamp)
            except (KeyError, TypeError, ValueError) as exc:
                errors = (f"Structured draft validation failed: {exc}",)
                draft = None
        if errors:
            result_status = OutcomeLearningAgentResultStatus.FAILED
            orchestration_status = OutcomeLearningOrchestrationStatus.FAILED
        elif warnings:
            result_status = OutcomeLearningAgentResultStatus.COMPLETED_WITH_WARNINGS
            orchestration_status = OutcomeLearningOrchestrationStatus.COMPLETED_WITH_WARNINGS
        else:
            result_status = OutcomeLearningAgentResultStatus.COMPLETED
            orchestration_status = OutcomeLearningOrchestrationStatus.COMPLETED
        result = OutcomeLearningAgentResult.create(
            result_id="",
            role=role,
            status=result_status,
            request_id=request.request_id,
            request_content_hash=request.content_hash,
            frozen_input_hash=request.frozen_input_hash,
            input_packet_hash=input_packet_hash,
            prompt_hash=prompt_hash,
            structured_output_hash=call.output_hash,
            draft_id=draft.draft_id if draft is not None else None,
            draft_content_hash=draft.content_hash if draft is not None else None,
            provider=request.provider,
            model=request.model,
            prompt_version=request.prompt_version,
            policy_version=request.policy_version,
            retry_count=call.retry_count,
            human_action_required=draft is not None,
            warnings=warnings,
            errors=errors,
            started_at_utc=timestamp,
            completed_at_utc=timestamp,
        )
        source_hashes = {
            "request": request.content_hash,
            "agent_result": result.content_hash,
        }
        if draft is not None:
            source_hashes["structured_draft"] = draft.content_hash
        orchestration = OutcomeLearningOrchestration.create(
            orchestration_id="",
            orchestration_version=orchestration_version,
            supersedes_orchestration_id=supersedes_orchestration_id,
            orchestration_kind=role,
            request=request,
            result=result,
            outcome_review_draft=(
                draft
                if isinstance(draft, OutcomeReviewDraft)
                else None
            ),
            mistake_lesson_proposal_draft=(
                draft
                if isinstance(draft, MistakeLessonProposalDraft)
                else None
            ),
            status=orchestration_status,
            human_action_required=draft is not None,
            source_hashes=source_hashes,
            warnings=warnings,
            validation_errors=errors,
            provider=request.provider,
            model=request.model,
            prompt_version=request.prompt_version,
            policy_version=request.policy_version,
            started_at_utc=timestamp,
            completed_at_utc=timestamp,
        )
        validation_errors = validate_outcome_learning_orchestration(orchestration)
        if validation_errors:
            raise ValueError(
                "Invalid OutcomeLearningOrchestration: "
                + " ".join(validation_errors)
            )
        self._stage(f"{role.value}:frozen")
        return orchestration

    def run_outcome_reviewer(
        self,
        request: OutcomeReviewerRequest,
        *,
        shadow_mode: bool = False,
        executed_at_utc: str | None = None,
        orchestration_version: int = 1,
        supersedes_orchestration_id: str | None = None,
    ) -> OutcomeLearningOrchestration:
        if not isinstance(request, OutcomeReviewerRequest):
            raise TypeError("request must be an OutcomeReviewerRequest.")
        self._verify_shadow(request, shadow_mode)
        packet = _outcome_reviewer_packet(request)
        return self._orchestration(
            request=request,
            role=OutcomeLearningAgentRole.OUTCOME_REVIEWER,
            packet=packet,
            system_prompt=_OUTCOME_REVIEWER_SYSTEM_PROMPT,
            schema=OUTCOME_REVIEWER_OUTPUT_SCHEMA,
            parser=_parse_outcome_review_draft,
            executed_at_utc=executed_at_utc or request.created_at_utc,
            orchestration_version=orchestration_version,
            supersedes_orchestration_id=supersedes_orchestration_id,
        )

    def run_mistake_memory_proposal(
        self,
        request: MistakeMemoryProposalRequest,
        *,
        shadow_mode: bool = False,
        executed_at_utc: str | None = None,
        orchestration_version: int = 1,
        supersedes_orchestration_id: str | None = None,
    ) -> OutcomeLearningOrchestration:
        if not isinstance(request, MistakeMemoryProposalRequest):
            raise TypeError("request must be a MistakeMemoryProposalRequest.")
        self._verify_shadow(request, shadow_mode)
        packet = _mistake_proposal_packet(request)
        return self._orchestration(
            request=request,
            role=OutcomeLearningAgentRole.MISTAKE_MEMORY_PROPOSAL,
            packet=packet,
            system_prompt=_MISTAKE_PROPOSAL_SYSTEM_PROMPT,
            schema=MISTAKE_MEMORY_PROPOSAL_OUTPUT_SCHEMA,
            parser=_parse_mistake_lesson_draft,
            executed_at_utc=executed_at_utc or request.created_at_utc,
            orchestration_version=orchestration_version,
            supersedes_orchestration_id=supersedes_orchestration_id,
        )


FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES = (
    "forecast_outcome_learning_orchestrations",
)
FORECAST_OUTCOME_LEARNING_ORCHESTRATION_INDEXES = (
    "forecast_outcome_learning_orchestrations_source_idx",
    "forecast_outcome_learning_orchestrations_status_idx",
    "forecast_outcome_learning_orchestrations_supersedes_idx",
)
FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TRIGGERS = (
    "forecast_outcome_learning_orchestrations_no_update",
    "forecast_outcome_learning_orchestrations_no_delete",
)

FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL = (
    """
    CREATE TABLE forecast_outcome_learning_orchestrations (
        orchestration_id TEXT PRIMARY KEY,
        orchestration_version INTEGER NOT NULL
            CHECK (orchestration_version >= 1),
        supersedes_orchestration_id TEXT NULL
            REFERENCES forecast_outcome_learning_orchestrations(orchestration_id)
            ON DELETE RESTRICT,
        orchestration_kind TEXT NOT NULL
            CHECK (orchestration_kind IN (
                'outcome_reviewer', 'mistake_memory_proposal'
            )),
        status TEXT NOT NULL
            CHECK (status IN (
                'completed', 'completed_with_warnings', 'failed'
            )),
        human_action_required INTEGER NOT NULL
            CHECK (human_action_required IN (0, 1)),
        forecast_id TEXT NOT NULL
            REFERENCES forecast_records(forecast_id) ON DELETE RESTRICT,
        observation_set_id TEXT NULL
            REFERENCES forecast_observation_sets(observation_set_id)
            ON DELETE RESTRICT,
        evaluation_id TEXT NOT NULL
            REFERENCES forecast_outcome_evaluations(evaluation_id)
            ON DELETE RESTRICT,
        outcome_review_id TEXT NULL
            REFERENCES forecast_outcome_reviews(review_id) ON DELETE RESTRICT,
        existing_lesson_id TEXT NULL
            REFERENCES mistake_memory_lessons(lesson_id) ON DELETE RESTRICT,
        request_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NULL,
        prompt_version TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        frozen_input_hash TEXT NOT NULL
            CHECK (
                length(frozen_input_hash) = 64
                AND frozen_input_hash NOT GLOB '*[^0-9a-f]*'
            ),
        structured_draft_hash TEXT NULL
            CHECK (
                structured_draft_hash IS NULL
                OR (
                    length(structured_draft_hash) = 64
                    AND structured_draft_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
        result_hash TEXT NOT NULL
            CHECK (
                length(result_hash) = 64
                AND result_hash NOT GLOB '*[^0-9a-f]*'
            ),
        warnings_json TEXT NOT NULL,
        validation_errors_json TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        completed_at_utc TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        calculation_version TEXT NOT NULL,
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        record_json TEXT NOT NULL,
        CHECK (
            supersedes_orchestration_id IS NULL
            OR supersedes_orchestration_id <> orchestration_id
        ),
        CHECK (
            (status = 'failed' AND structured_draft_hash IS NULL)
            OR (
                status IN ('completed', 'completed_with_warnings')
                AND structured_draft_hash IS NOT NULL
                AND human_action_required = 1
            )
        ),
        CHECK (
            (
                orchestration_kind = 'outcome_reviewer'
                AND observation_set_id IS NOT NULL
                AND outcome_review_id IS NULL
                AND existing_lesson_id IS NULL
            )
            OR (
                orchestration_kind = 'mistake_memory_proposal'
                AND observation_set_id IS NULL
                AND outcome_review_id IS NOT NULL
            )
        )
    )
    """,
    """
    CREATE INDEX forecast_outcome_learning_orchestrations_source_idx
    ON forecast_outcome_learning_orchestrations(
        forecast_id, evaluation_id, orchestration_kind,
        orchestration_version, orchestration_id
    )
    """,
    """
    CREATE INDEX forecast_outcome_learning_orchestrations_status_idx
    ON forecast_outcome_learning_orchestrations(
        status, human_action_required, completed_at_utc, orchestration_id
    )
    """,
    """
    CREATE UNIQUE INDEX forecast_outcome_learning_orchestrations_supersedes_idx
    ON forecast_outcome_learning_orchestrations(supersedes_orchestration_id)
    WHERE supersedes_orchestration_id IS NOT NULL
    """,
    """
    CREATE TRIGGER forecast_outcome_learning_orchestrations_no_update
    BEFORE UPDATE ON forecast_outcome_learning_orchestrations
    BEGIN
        SELECT RAISE(
            ABORT,
            'forecast_outcome_learning_orchestrations is append-only'
        );
    END
    """,
    """
    CREATE TRIGGER forecast_outcome_learning_orchestrations_no_delete
    BEFORE DELETE ON forecast_outcome_learning_orchestrations
    BEGIN
        SELECT RAISE(
            ABORT,
            'forecast_outcome_learning_orchestrations is append-only'
        );
    END
    """,
)


__all__ = [
    "DIAGNOSIS_CANDIDATE_SCHEMA_VERSION",
    "DUPLICATE_LESSON_CANDIDATE_SCHEMA_VERSION",
    "FORECAST_OUTCOME_LEARNING_ORCHESTRATION_INDEXES",
    "FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL",
    "FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES",
    "FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TRIGGERS",
    "MISTAKE_LESSON_DRAFT_SCHEMA_VERSION",
    "MISTAKE_MEMORY_PROPOSAL_OUTPUT_SCHEMA",
    "MISTAKE_PROPOSAL_PROMPT_VERSION",
    "MISTAKE_PROPOSAL_REQUEST_SCHEMA_VERSION",
    "OUTCOME_LEARNING_CALCULATION_VERSION",
    "OUTCOME_LEARNING_ORCHESTRATION_SCHEMA_VERSION",
    "OUTCOME_LEARNING_POLICY_VERSION",
    "OUTCOME_LEARNING_RESULT_SCHEMA_VERSION",
    "OUTCOME_LEARNING_ROLE_SCHEMA_VERSION",
    "OUTCOME_REVIEWER_OUTPUT_SCHEMA",
    "OUTCOME_REVIEWER_PROMPT_VERSION",
    "OUTCOME_REVIEWER_REQUEST_SCHEMA_VERSION",
    "OUTCOME_REVIEW_DRAFT_SCHEMA_VERSION",
    "DiagnosisCandidate",
    "DuplicateLessonCandidate",
    "MistakeLessonProposalDraft",
    "MistakeMemoryProposalRequest",
    "OutcomeLearningAgentOrchestrator",
    "OutcomeLearningAgentResult",
    "OutcomeLearningAgentResultStatus",
    "OutcomeLearningAgentRole",
    "OutcomeLearningOrchestration",
    "OutcomeLearningOrchestrationStatus",
    "OutcomeReviewDraft",
    "OutcomeReviewerRequest",
    "build_mistake_memory_proposal_request",
    "build_outcome_reviewer_request",
    "canonical_outcome_learning_json",
    "outcome_learning_agent_result_content_hash",
    "outcome_learning_orchestration_content_hash",
    "replay_outcome_learning_orchestration",
    "validate_outcome_learning_orchestration",
]
