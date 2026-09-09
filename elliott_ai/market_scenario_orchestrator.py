"""Offline end-to-end orchestration for the Market Scenario Engine.

This module coordinates already-implemented deterministic and model-backed
stages. It does not research, reason about markets, modify Elliott analysis,
persist data, render reports, or make trading decisions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .current_scenario_generator import (
    CURRENT_SCENARIO_ADVERSARIAL_PROMPT_VERSION,
    CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION,
    CurrentMarketScenarioGenerator,
)
from .current_scenarios import (
    CurrentScenarioGenerationResult,
    CurrentScenarioSet,
    ScenarioAdversarialRecommendation,
    ScenarioType,
    current_scenario_set_content_hash,
    validate_current_scenario_generation_result,
    validate_current_scenario_set,
)
from .current_state import (
    CurrentMarketState,
    CurrentStateBuildResult,
    CurrentStateBuilder,
    FeatureAvailability,
    current_market_state_content_hash,
    current_state_build_result_content_hash,
    evidence_packet_content_hash,
    exposure_report_content_hash,
    material_event_packet_content_hash,
    validate_current_market_state,
)
from .historical_market import (
    MarketRegime,
    MarketRegimeState,
    RelativePerformanceBucket,
)
from .historical_patterns import (
    HistoricalPatternLibrary,
    PatternStatus,
    historical_pattern_library_content_hash,
    validate_historical_pattern_library,
)
from .market_scenario import (
    EvidenceItem,
    ExposureCategoryCoverage,
    ExposureItem,
    FrozenTechnicalPremise,
    MaterialEvent,
    ValidationIssue,
    ValidationResult,
    frozen_technical_premise_content_hash,
)
from .pattern_retrieval import (
    DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE,
    MatchQuality,
    PatternRetrievalEngine,
    PatternRetrievalExecutionResult,
    PatternRetrievalResult,
    PatternRetrievalScoringProfile,
    create_pattern_retrieval_query,
    pattern_retrieval_execution_result_content_hash,
    pattern_retrieval_result_content_hash,
    validate_pattern_retrieval_result,
    validate_pattern_retrieval_scoring_profile,
)
from .providers import AnalysisProvider
from .reasoning_audit import (
    AuditCoverageMetrics,
    ReasoningAuditGraph,
    audit_coverage_metrics_content_hash,
    build_reasoning_audit_graph,
    reasoning_audit_graph_content_hash,
    validate_reasoning_audit_graph,
)


MARKET_SCENARIO_ORCHESTRATION_INPUT_SCHEMA_VERSION = (
    "market-scenario-orchestration-input-1.0.0"
)
MARKET_SCENARIO_ORCHESTRATION_RESULT_SCHEMA_VERSION = (
    "market-scenario-orchestration-result-1.0.0"
)
MARKET_SCENARIO_STAGE_RECORD_SCHEMA_VERSION = (
    "market-scenario-stage-record-1.0.0"
)
MARKET_SCENARIO_REPLAY_PACKET_SCHEMA_VERSION = (
    "market-scenario-replay-packet-1.0.0"
)
MARKET_SCENARIO_REPLAY_RESULT_SCHEMA_VERSION = (
    "market-scenario-replay-result-1.0.0"
)
PATTERN_RETRIEVAL_CONFIGURATION_SCHEMA_VERSION = (
    "pattern-retrieval-configuration-1.0.0"
)
SCENARIO_GENERATION_CONFIGURATION_SCHEMA_VERSION = (
    "scenario-generation-configuration-1.0.0"
)
MARKET_SCENARIO_ORCHESTRATION_VALIDATION_VERSION = (
    "market-scenario-orchestration-validation-1.0.0"
)
MARKET_SCENARIO_REPLAY_VALIDATION_VERSION = (
    "market-scenario-replay-validation-1.0.0"
)
MARKET_SCENARIO_ORCHESTRATOR_VERSION = (
    "market-scenario-orchestrator-1.0.0"
)


class OrchestrationStage(StrEnum):
    INPUT_VALIDATION = "input_validation"
    CURRENT_STATE_BUILD = "current_state_build"
    PATTERN_RETRIEVAL = "pattern_retrieval"
    SCENARIO_GENERATION = "scenario_generation"
    FINAL_VALIDATION = "final_validation"
    COMPLETED = "completed"


class OrchestrationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    OFFLINE_FIXTURE = "offline_fixture"
    PACKET_REPLAY = "packet_replay"
    CONFIGURED_PROVIDER = "configured_provider"


_STAGE_ORDER = MappingProxyType(
    {
        OrchestrationStage.INPUT_VALIDATION: 0,
        OrchestrationStage.CURRENT_STATE_BUILD: 1,
        OrchestrationStage.PATTERN_RETRIEVAL: 2,
        OrchestrationStage.SCENARIO_GENERATION: 3,
        OrchestrationStage.FINAL_VALIDATION: 4,
        OrchestrationStage.COMPLETED: 5,
    }
)
_ANALYTICAL_STAGES = frozenset(
    {
        OrchestrationStage.INPUT_VALIDATION,
        OrchestrationStage.CURRENT_STATE_BUILD,
        OrchestrationStage.PATTERN_RETRIEVAL,
        OrchestrationStage.SCENARIO_GENERATION,
    }
)
_TERMINAL_STAGES = frozenset(
    {
        OrchestrationStage.FINAL_VALIDATION,
        OrchestrationStage.COMPLETED,
    }
)
_SUCCESS_STATUSES = frozenset(
    {
        OrchestrationStatus.COMPLETED,
        OrchestrationStatus.COMPLETED_WITH_WARNINGS,
    }
)
_FAILURE_STATUSES = frozenset(
    {
        OrchestrationStatus.FAILED,
        OrchestrationStatus.INSUFFICIENT_EVIDENCE,
        OrchestrationStatus.CANCELLED,
    }
)


def _normalize_timestamp(
    value: str | datetime,
    *,
    field_name: str,
) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be an ISO-8601 timestamp."
            ) from exc
    else:
        raise TypeError(
            f"{field_name} must be a datetime or timestamp string."
        )
    aware = (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _temporal_point(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _normalized_token(value: str) -> str:
    return re.sub(
        r"_+",
        "_",
        re.sub(r"[^a-z0-9]+", "_", value.casefold()),
    ).strip("_")


def _enum_value(
    value: Any,
    enum_type: type[StrEnum],
    *,
    field_name: str,
) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be a valid {enum_type.__name__}."
        ) from exc


def _enum_tuple(
    value: Sequence[Any] | None,
    enum_type: type[StrEnum],
    *,
    field_name: str,
) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    return tuple(
        _enum_value(item, enum_type, field_name=field_name)
        for item in value
    )


def _string_tuple(
    value: Sequence[str] | None,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"{field_name} must contain only strings.")
    return result


def _model_tuple(
    value: Sequence[Any] | None,
    model_type: type,
    *,
    field_name: str,
) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence.")
    result = tuple(value)
    if not all(isinstance(item, model_type) for item in result):
        raise TypeError(
            f"{field_name} must contain only {model_type.__name__} values."
        )
    return result


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in sorted(
                    value.items(),
                    key=lambda pair: str(pair[0]),
                )
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"JSON contract cannot contain {type(value).__name__} values."
    )


def _string_mapping(
    value: Mapping[str, str] | None,
    *,
    field_name: str,
) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    raw = dict(value)
    if not all(
        isinstance(key, str)
        and key
        and isinstance(item, str)
        for key, item in raw.items()
    ):
        raise TypeError(
            f"{field_name} requires non-empty string keys and string values."
        )
    return MappingProxyType(dict(sorted(raw.items())))


def _feature_mapping(
    value: Mapping[str, FeatureAvailability | str] | None,
) -> Mapping[str, FeatureAvailability]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("availability_overrides must be a mapping.")
    result: dict[str, FeatureAvailability] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError(
                "availability_overrides requires non-empty string keys."
            )
        result[key] = _enum_value(
            item,
            FeatureAvailability,
            field_name="availability_overrides",
        )
    return MappingProxyType(dict(sorted(result.items())))


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _contract_hash(value: Any) -> str:
    payload = dict(value.to_dict())
    payload.pop("content_hash", None)
    return _hash_payload(payload)


def _mapping(
    value: Mapping[str, Any],
    *,
    model_name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{model_name}.from_dict requires a mapping.")
    return dict(value)


def _reject_unknown(
    value: Mapping[str, Any],
    allowed: set[str],
    *,
    model_name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{model_name} contains unknown fields: "
            + ", ".join(unknown)
            + "."
        )


def _duplicates(values: Sequence[str]) -> set[str]:
    return {
        value
        for value, count in Counter(values).items()
        if count > 1
    }


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True)
class PatternRetrievalConfiguration(_JsonContract):
    requested_limit: int = 10
    counter_pattern_limit: int = 3
    minimum_match_quality: MatchQuality = MatchQuality.VERY_WEAK
    include_context_dependent_patterns: bool = False
    include_bidirectional_patterns: bool = False
    allowed_pattern_statuses: tuple[PatternStatus, ...] = (
        PatternStatus.REVIEWED,
        PatternStatus.VALIDATED,
    )
    scoring_profile_version: str = (
        DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE.profile_version
    )
    content_hash: str = ""
    schema_version: str = (
        PATTERN_RETRIEVAL_CONFIGURATION_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_match_quality",
            _enum_value(
                self.minimum_match_quality,
                MatchQuality,
                field_name="minimum_match_quality",
            ),
        )
        object.__setattr__(
            self,
            "allowed_pattern_statuses",
            tuple(
                sorted(
                    set(
                        _enum_tuple(
                            self.allowed_pattern_statuses,
                            PatternStatus,
                            field_name="allowed_pattern_statuses",
                        )
                    ),
                    key=lambda item: item.value,
                )
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "PatternRetrievalConfiguration":
        supplied = values.pop("content_hash", "")
        if supplied:
            raise ValueError(
                "create() calculates content_hash; do not supply it."
            )
        seed = cls(**values, content_hash="")
        return replace(
            seed,
            content_hash=pattern_retrieval_configuration_content_hash(
                seed
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "PatternRetrievalConfiguration":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ScenarioGenerationConfiguration(_JsonContract):
    minimum_scenarios: int = 3
    maximum_scenarios: int = 6
    require_counter_scenario: bool = True
    require_multifactor_scenario: bool = True
    require_adversarial_pass: bool = True
    maximum_correction_attempts: int = 2
    prompt_version: str = CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION
    content_hash: str = ""
    schema_version: str = (
        SCENARIO_GENERATION_CONFIGURATION_SCHEMA_VERSION
    )

    @classmethod
    def create(cls, **values: Any) -> "ScenarioGenerationConfiguration":
        supplied = values.pop("content_hash", "")
        if supplied:
            raise ValueError(
                "create() calculates content_hash; do not supply it."
            )
        seed = cls(**values, content_hash="")
        return replace(
            seed,
            content_hash=scenario_generation_configuration_content_hash(
                seed
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ScenarioGenerationConfiguration":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class StageExecutionRecord(_JsonContract):
    stage: OrchestrationStage
    status: OrchestrationStatus
    started_at: str
    completed_at: str
    input_hashes: Mapping[str, str]
    output_hashes: Mapping[str, str]
    validation_result: ValidationResult
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    attempts: int
    provider: str | None
    model_name: str | None
    prompt_version: str | None
    analytical_hash: str = ""
    content_hash: str = ""
    schema_version: str = MARKET_SCENARIO_STAGE_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stage",
            _enum_value(
                self.stage,
                OrchestrationStage,
                field_name="stage",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                OrchestrationStatus,
                field_name="status",
            ),
        )
        for name in ("started_at", "completed_at"):
            object.__setattr__(
                self,
                name,
                _normalize_timestamp(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        object.__setattr__(
            self,
            "input_hashes",
            _string_mapping(
                self.input_hashes,
                field_name="input_hashes",
            ),
        )
        object.__setattr__(
            self,
            "output_hashes",
            _string_mapping(
                self.output_hashes,
                field_name="output_hashes",
            ),
        )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError(
                "validation_result must be ValidationResult."
            )
        for name in ("warnings", "errors"):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 0
        ):
            raise ValueError(
                "attempts must be a non-negative integer."
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "StageExecutionRecord":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MarketScenarioOrchestrationInput(_JsonContract):
    orchestration_input_id: str
    symbol: str
    applicable_cutoff: str
    frozen_technical_premise: FrozenTechnicalPremise
    current_evidence: tuple[EvidenceItem, ...]
    current_exposures: tuple[ExposureItem, ...]
    exposure_category_coverage: tuple[
        ExposureCategoryCoverage, ...
    ]
    current_events: tuple[MaterialEvent, ...]
    historical_pattern_library: HistoricalPatternLibrary
    current_regime_inputs: MarketRegime | None
    valuation_inputs: MarketRegimeState | None
    liquidity_inputs: MarketRegimeState | None
    positioning_inputs: MarketRegimeState | None
    benchmark_relative_inputs: RelativePerformanceBucket | None
    sector_relative_inputs: RelativePerformanceBucket | None
    retrieval_configuration: PatternRetrievalConfiguration
    scenario_generation_configuration: ScenarioGenerationConfiguration
    execution_mode: ExecutionMode
    created_at: str
    availability_overrides: Mapping[
        str, FeatureAvailability
    ] = field(default_factory=dict)
    state_assumptions: tuple[str, ...] = ()
    provider_configuration: Mapping[str, str] = field(
        default_factory=dict
    )
    content_hash: str = ""
    schema_version: str = (
        MARKET_SCENARIO_ORCHESTRATION_INPUT_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "applicable_cutoff",
            _normalize_timestamp(
                self.applicable_cutoff,
                field_name="applicable_cutoff",
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(
                self.created_at,
                field_name="created_at",
            ),
        )
        if not isinstance(
            self.frozen_technical_premise,
            FrozenTechnicalPremise,
        ):
            raise TypeError(
                "frozen_technical_premise must be FrozenTechnicalPremise."
            )
        for name, model_type in (
            ("current_evidence", EvidenceItem),
            ("current_exposures", ExposureItem),
            (
                "exposure_category_coverage",
                ExposureCategoryCoverage,
            ),
            ("current_events", MaterialEvent),
        ):
            object.__setattr__(
                self,
                name,
                _model_tuple(
                    getattr(self, name),
                    model_type,
                    field_name=name,
                ),
            )
        if not isinstance(
            self.historical_pattern_library,
            HistoricalPatternLibrary,
        ):
            raise TypeError(
                "historical_pattern_library must be HistoricalPatternLibrary."
            )
        if self.current_regime_inputs is not None and not isinstance(
            self.current_regime_inputs,
            MarketRegime,
        ):
            raise TypeError(
                "current_regime_inputs must be MarketRegime or null."
            )
        for name in (
            "valuation_inputs",
            "liquidity_inputs",
            "positioning_inputs",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _enum_value(
                        value,
                        MarketRegimeState,
                        field_name=name,
                    ),
                )
        for name in (
            "benchmark_relative_inputs",
            "sector_relative_inputs",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _enum_value(
                        value,
                        RelativePerformanceBucket,
                        field_name=name,
                    ),
                )
        if not isinstance(
            self.retrieval_configuration,
            PatternRetrievalConfiguration,
        ):
            raise TypeError(
                "retrieval_configuration must be PatternRetrievalConfiguration."
            )
        if not isinstance(
            self.scenario_generation_configuration,
            ScenarioGenerationConfiguration,
        ):
            raise TypeError(
                "scenario_generation_configuration must be ScenarioGenerationConfiguration."
            )
        object.__setattr__(
            self,
            "execution_mode",
            _enum_value(
                self.execution_mode,
                ExecutionMode,
                field_name="execution_mode",
            ),
        )
        object.__setattr__(
            self,
            "availability_overrides",
            _feature_mapping(self.availability_overrides),
        )
        object.__setattr__(
            self,
            "state_assumptions",
            _string_tuple(
                self.state_assumptions,
                field_name="state_assumptions",
            ),
        )
        object.__setattr__(
            self,
            "provider_configuration",
            _string_mapping(
                self.provider_configuration,
                field_name="provider_configuration",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        orchestration_input_id: str | None = None,
        **values: Any,
    ) -> "MarketScenarioOrchestrationInput":
        supplied = values.pop("content_hash", "")
        if supplied:
            raise ValueError(
                "create() calculates content_hash; do not supply it."
            )
        created_at = _normalize_timestamp(
            values["created_at"],
            field_name="created_at",
        )
        premise = values["frozen_technical_premise"]
        library = values["historical_pattern_library"]
        selected_id = (
            orchestration_input_id.strip()
            if orchestration_input_id is not None
            else "orchestration_input_"
            + _hash_payload(
                {
                    "symbol": values["symbol"],
                    "applicable_cutoff": values[
                        "applicable_cutoff"
                    ],
                    "premise_hash": premise.frozen_content_hash,
                    "library_hash": library.content_hash,
                    "created_at": created_at,
                }
            )[:20]
        )
        seed = cls(
            orchestration_input_id=selected_id,
            **values,
            content_hash="",
        )
        return replace(
            seed,
            content_hash=market_scenario_orchestration_input_content_hash(
                seed
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "MarketScenarioOrchestrationInput":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["frozen_technical_premise"] = (
            FrozenTechnicalPremise.from_dict(
                raw["frozen_technical_premise"]
            )
        )
        raw["current_evidence"] = tuple(
            EvidenceItem.from_dict(item)
            for item in raw.get("current_evidence", ())
        )
        raw["current_exposures"] = tuple(
            ExposureItem.from_dict(item)
            for item in raw.get("current_exposures", ())
        )
        raw["exposure_category_coverage"] = tuple(
            ExposureCategoryCoverage.from_dict(item)
            for item in raw.get(
                "exposure_category_coverage",
                (),
            )
        )
        raw["current_events"] = tuple(
            MaterialEvent.from_dict(item)
            for item in raw.get("current_events", ())
        )
        raw["historical_pattern_library"] = (
            HistoricalPatternLibrary.from_dict(
                raw["historical_pattern_library"]
            )
        )
        if raw.get("current_regime_inputs") is not None:
            raw["current_regime_inputs"] = MarketRegime.from_dict(
                raw["current_regime_inputs"]
            )
        raw["retrieval_configuration"] = (
            PatternRetrievalConfiguration.from_dict(
                raw["retrieval_configuration"]
            )
        )
        raw["scenario_generation_configuration"] = (
            ScenarioGenerationConfiguration.from_dict(
                raw["scenario_generation_configuration"]
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MarketScenarioOrchestrationResult(_JsonContract):
    orchestration_id: str
    success: bool
    status: OrchestrationStatus
    symbol: str
    applicable_cutoff: str
    orchestration_input_hash: str
    frozen_technical_premise_hash: str
    current_state: CurrentMarketState | None
    pattern_retrieval_result: PatternRetrievalResult | None
    current_scenario_set: CurrentScenarioSet | None
    state_build_result: CurrentStateBuildResult | None
    retrieval_execution_result: (
        PatternRetrievalExecutionResult | None
    )
    scenario_generation_result: (
        CurrentScenarioGenerationResult | None
    )
    reasoning_audit_graph: ReasoningAuditGraph | None
    audit_coverage_metrics: AuditCoverageMetrics | None
    stage_records: tuple[StageExecutionRecord, ...]
    final_validation_result: ValidationResult
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    started_at: str
    completed_at: str
    duration_ms: int
    execution_mode: ExecutionMode
    provider: str
    model_name: str
    prompt_versions: Mapping[str, str]
    analytical_hash: str = ""
    content_hash: str = ""
    schema_version: str = (
        MARKET_SCENARIO_ORCHESTRATION_RESULT_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum_value(
                self.status,
                OrchestrationStatus,
                field_name="status",
            ),
        )
        object.__setattr__(
            self,
            "execution_mode",
            _enum_value(
                self.execution_mode,
                ExecutionMode,
                field_name="execution_mode",
            ),
        )
        for name in ("applicable_cutoff", "started_at", "completed_at"):
            object.__setattr__(
                self,
                name,
                _normalize_timestamp(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        for value, model_type, name in (
            (self.current_state, CurrentMarketState, "current_state"),
            (
                self.pattern_retrieval_result,
                PatternRetrievalResult,
                "pattern_retrieval_result",
            ),
            (
                self.current_scenario_set,
                CurrentScenarioSet,
                "current_scenario_set",
            ),
            (
                self.state_build_result,
                CurrentStateBuildResult,
                "state_build_result",
            ),
            (
                self.retrieval_execution_result,
                PatternRetrievalExecutionResult,
                "retrieval_execution_result",
            ),
            (
                self.scenario_generation_result,
                CurrentScenarioGenerationResult,
                "scenario_generation_result",
            ),
            (
                self.reasoning_audit_graph,
                ReasoningAuditGraph,
                "reasoning_audit_graph",
            ),
            (
                self.audit_coverage_metrics,
                AuditCoverageMetrics,
                "audit_coverage_metrics",
            ),
        ):
            if value is not None and not isinstance(value, model_type):
                raise TypeError(
                    f"{name} must be {model_type.__name__} or null."
                )
        object.__setattr__(
            self,
            "stage_records",
            _model_tuple(
                self.stage_records,
                StageExecutionRecord,
                field_name="stage_records",
            ),
        )
        if not isinstance(
            self.final_validation_result,
            ValidationResult,
        ):
            raise TypeError(
                "final_validation_result must be ValidationResult."
            )
        for name in ("warnings", "errors"):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError(
                "duration_ms must be a non-negative integer."
            )
        object.__setattr__(
            self,
            "prompt_versions",
            _string_mapping(
                self.prompt_versions,
                field_name="prompt_versions",
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "MarketScenarioOrchestrationResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        model_fields = (
            ("current_state", CurrentMarketState),
            ("pattern_retrieval_result", PatternRetrievalResult),
            ("current_scenario_set", CurrentScenarioSet),
            ("state_build_result", CurrentStateBuildResult),
            (
                "retrieval_execution_result",
                PatternRetrievalExecutionResult,
            ),
            (
                "scenario_generation_result",
                CurrentScenarioGenerationResult,
            ),
            ("reasoning_audit_graph", ReasoningAuditGraph),
            ("audit_coverage_metrics", AuditCoverageMetrics),
        )
        for name, model_type in model_fields:
            if raw.get(name) is not None:
                raw[name] = model_type.from_dict(raw[name])
        raw["stage_records"] = tuple(
            StageExecutionRecord.from_dict(item)
            for item in raw.get("stage_records", ())
        )
        raw["final_validation_result"] = ValidationResult.from_dict(
            raw["final_validation_result"]
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MarketScenarioReplayPacket(_JsonContract):
    packet_id: str
    orchestration_input: MarketScenarioOrchestrationInput
    expected_input_hash: str
    fixture_provider_responses: tuple[Mapping[str, Any], ...]
    expected_stage_hashes: Mapping[str, str]
    expected_final_hash: str | None
    created_at: str
    fixture_model_name: str = "phase8-replay-fixture"
    content_hash: str = ""
    packet_schema_version: str = (
        MARKET_SCENARIO_REPLAY_PACKET_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.orchestration_input,
            MarketScenarioOrchestrationInput,
        ):
            raise TypeError(
                "orchestration_input must be MarketScenarioOrchestrationInput."
            )
        if (
            isinstance(self.fixture_provider_responses, (str, bytes))
            or not isinstance(
                self.fixture_provider_responses,
                Sequence,
            )
        ):
            raise TypeError(
                "fixture_provider_responses must be a sequence."
            )
        responses: list[Mapping[str, Any]] = []
        for item in self.fixture_provider_responses:
            if not isinstance(item, Mapping):
                raise TypeError(
                    "fixture_provider_responses must contain mappings."
                )
            responses.append(_freeze_json(item))
        object.__setattr__(
            self,
            "fixture_provider_responses",
            tuple(responses),
        )
        object.__setattr__(
            self,
            "expected_stage_hashes",
            _string_mapping(
                self.expected_stage_hashes,
                field_name="expected_stage_hashes",
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(
                self.created_at,
                field_name="created_at",
            ),
        )

    @classmethod
    def create(cls, **values: Any) -> "MarketScenarioReplayPacket":
        supplied = values.pop("content_hash", "")
        if supplied:
            raise ValueError(
                "create() calculates content_hash; do not supply it."
            )
        seed = cls(**values, content_hash="")
        return replace(
            seed,
            content_hash=market_scenario_replay_packet_content_hash(
                seed
            ),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "MarketScenarioReplayPacket":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        raw["orchestration_input"] = (
            MarketScenarioOrchestrationInput.from_dict(
                raw["orchestration_input"]
            )
        )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class MarketScenarioReplayResult(_JsonContract):
    packet_id: str
    success: bool
    orchestration_result: MarketScenarioOrchestrationResult | None
    validation_result: ValidationResult
    expected_input_hash: str
    actual_input_hash: str
    expected_stage_hashes: Mapping[str, str]
    actual_stage_hashes: Mapping[str, str]
    expected_final_hash: str | None
    actual_final_hash: str | None
    mismatch_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    remaining_fixture_responses: int
    content_hash: str = ""
    schema_version: str = MARKET_SCENARIO_REPLAY_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.orchestration_result is not None and not isinstance(
            self.orchestration_result,
            MarketScenarioOrchestrationResult,
        ):
            raise TypeError(
                "orchestration_result must be MarketScenarioOrchestrationResult or null."
            )
        if not isinstance(self.validation_result, ValidationResult):
            raise TypeError(
                "validation_result must be ValidationResult."
            )
        for name in (
            "expected_stage_hashes",
            "actual_stage_hashes",
        ):
            object.__setattr__(
                self,
                name,
                _string_mapping(
                    getattr(self, name),
                    field_name=name,
                ),
            )
        for name in ("mismatch_codes", "warnings"):
            object.__setattr__(
                self,
                name,
                _string_tuple(getattr(self, name), field_name=name),
            )
        if (
            isinstance(self.remaining_fixture_responses, bool)
            or not isinstance(
                self.remaining_fixture_responses,
                int,
            )
            or self.remaining_fixture_responses < 0
        ):
            raise ValueError(
                "remaining_fixture_responses must be non-negative."
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "MarketScenarioReplayResult":
        raw = _mapping(value, model_name=cls.__name__)
        _reject_unknown(
            raw,
            {item.name for item in fields(cls)},
            model_name=cls.__name__,
        )
        if raw.get("orchestration_result") is not None:
            raw["orchestration_result"] = (
                MarketScenarioOrchestrationResult.from_dict(
                    raw["orchestration_result"]
                )
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        return cls(**raw)


def pattern_retrieval_configuration_content_hash(
    config: PatternRetrievalConfiguration,
) -> str:
    if not isinstance(config, PatternRetrievalConfiguration):
        raise TypeError(
            "config must be PatternRetrievalConfiguration."
        )
    return _contract_hash(config)


def scenario_generation_configuration_content_hash(
    config: ScenarioGenerationConfiguration,
) -> str:
    if not isinstance(config, ScenarioGenerationConfiguration):
        raise TypeError(
            "config must be ScenarioGenerationConfiguration."
        )
    return _contract_hash(config)


def stage_execution_record_analytical_hash(
    record: StageExecutionRecord,
) -> str:
    if not isinstance(record, StageExecutionRecord):
        raise TypeError("record must be StageExecutionRecord.")
    payload = record.to_dict()
    for name in (
        "started_at",
        "completed_at",
        "analytical_hash",
        "content_hash",
    ):
        payload.pop(name, None)
    return _hash_payload(payload)


def stage_execution_record_content_hash(
    record: StageExecutionRecord,
) -> str:
    if not isinstance(record, StageExecutionRecord):
        raise TypeError("record must be StageExecutionRecord.")
    return _contract_hash(record)


def market_scenario_orchestration_input_content_hash(
    orchestration_input: MarketScenarioOrchestrationInput,
) -> str:
    if not isinstance(
        orchestration_input,
        MarketScenarioOrchestrationInput,
    ):
        raise TypeError(
            "orchestration_input must be MarketScenarioOrchestrationInput."
        )
    return _contract_hash(orchestration_input)


def market_scenario_orchestration_result_analytical_hash(
    result: MarketScenarioOrchestrationResult,
) -> str:
    if not isinstance(result, MarketScenarioOrchestrationResult):
        raise TypeError(
            "result must be MarketScenarioOrchestrationResult."
        )
    payload = result.to_dict()
    for name in (
        "started_at",
        "completed_at",
        "duration_ms",
        "analytical_hash",
        "content_hash",
    ):
        payload.pop(name, None)
    payload["stage_records"] = [
        {
            "stage": record.stage.value,
            "analytical_hash": record.analytical_hash,
        }
        for record in result.stage_records
    ]
    return _hash_payload(payload)


def market_scenario_orchestration_result_content_hash(
    result: MarketScenarioOrchestrationResult,
) -> str:
    if not isinstance(result, MarketScenarioOrchestrationResult):
        raise TypeError(
            "result must be MarketScenarioOrchestrationResult."
        )
    return _contract_hash(result)


def market_scenario_replay_packet_content_hash(
    packet: MarketScenarioReplayPacket,
) -> str:
    if not isinstance(packet, MarketScenarioReplayPacket):
        raise TypeError(
            "packet must be MarketScenarioReplayPacket."
        )
    return _contract_hash(packet)


def market_scenario_replay_result_content_hash(
    result: MarketScenarioReplayResult,
) -> str:
    if not isinstance(result, MarketScenarioReplayResult):
        raise TypeError(
            "result must be MarketScenarioReplayResult."
        )
    return _contract_hash(result)


def _new_issue(
    code: str,
    path: str,
    message: str,
    *,
    severity: str = "error",
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        path=path,
        message=message,
    )


def _validation_from_issues(
    issues: Sequence[ValidationIssue],
    *,
    validation_version: str = (
        MARKET_SCENARIO_ORCHESTRATION_VALIDATION_VERSION
    ),
) -> ValidationResult:
    errors = tuple(
        item for item in issues if item.severity == "error"
    )
    warnings = tuple(
        item for item in issues if item.severity != "error"
    )
    codes = tuple(dict.fromkeys(item.code for item in errors))
    return ValidationResult(
        is_valid=not errors,
        score=100.0 if not errors else 0.0,
        errors=errors,
        warnings=warnings,
        failed_rules=codes,
        passed_rules=() if errors else ("validation_passed",),
        validation_version=validation_version,
    )


def _validate_retrieval_configuration(
    config: PatternRetrievalConfiguration,
    scoring_profile: PatternRetrievalScoringProfile,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if (
        config.schema_version
        != PATTERN_RETRIEVAL_CONFIGURATION_SCHEMA_VERSION
    ):
        issues.append(
            _new_issue(
                "unsupported_retrieval_configuration",
                "retrieval_configuration.schema_version",
                "Pattern-retrieval configuration schema is unsupported.",
            )
        )
    if (
        not config.content_hash
        or config.content_hash
        != pattern_retrieval_configuration_content_hash(config)
    ):
        issues.append(
            _new_issue(
                "invalid_input_hash",
                "retrieval_configuration.content_hash",
                "Pattern-retrieval configuration hash is invalid.",
            )
        )
    if (
        isinstance(config.requested_limit, bool)
        or not isinstance(config.requested_limit, int)
        or config.requested_limit < 1
        or config.requested_limit > scoring_profile.maximum_results
    ):
        issues.append(
            _new_issue(
                "unsupported_retrieval_configuration",
                "retrieval_configuration.requested_limit",
                "Requested limit is outside the scoring profile bounds.",
            )
        )
    if (
        isinstance(config.counter_pattern_limit, bool)
        or not isinstance(config.counter_pattern_limit, int)
        or config.counter_pattern_limit < 0
        or config.counter_pattern_limit
        > scoring_profile.maximum_counter_results
    ):
        issues.append(
            _new_issue(
                "unsupported_retrieval_configuration",
                "retrieval_configuration.counter_pattern_limit",
                "Counter-pattern limit is outside the scoring profile bounds.",
            )
        )
    if not config.allowed_pattern_statuses:
        issues.append(
            _new_issue(
                "unsupported_retrieval_configuration",
                "retrieval_configuration.allowed_pattern_statuses",
                "At least one explicit pattern status is required.",
            )
        )
    if (
        config.scoring_profile_version
        != scoring_profile.profile_version
    ):
        issues.append(
            _new_issue(
                "unsupported_retrieval_configuration",
                "retrieval_configuration.scoring_profile_version",
                "Retrieval configuration does not match the active scoring profile.",
            )
        )
    return tuple(issues)


def _validate_scenario_configuration(
    config: ScenarioGenerationConfiguration,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if (
        config.schema_version
        != SCENARIO_GENERATION_CONFIGURATION_SCHEMA_VERSION
    ):
        issues.append(
            _new_issue(
                "unsupported_scenario_configuration",
                "scenario_generation_configuration.schema_version",
                "Scenario-generation configuration schema is unsupported.",
            )
        )
    if (
        not config.content_hash
        or config.content_hash
        != scenario_generation_configuration_content_hash(config)
    ):
        issues.append(
            _new_issue(
                "invalid_input_hash",
                "scenario_generation_configuration.content_hash",
                "Scenario-generation configuration hash is invalid.",
            )
        )
    if (
        config.minimum_scenarios != 3
        or config.maximum_scenarios != 6
        or not config.require_counter_scenario
        or not config.require_multifactor_scenario
    ):
        issues.append(
            _new_issue(
                "unsupported_scenario_configuration",
                "scenario_generation_configuration",
                "Phase 8 must preserve the existing three-to-six scenario, counter-scenario, and multi-factor contracts.",
            )
        )
    if (
        isinstance(config.maximum_correction_attempts, bool)
        or not isinstance(
            config.maximum_correction_attempts,
            int,
        )
        or not 1 <= config.maximum_correction_attempts <= 3
    ):
        issues.append(
            _new_issue(
                "unsupported_scenario_configuration",
                "scenario_generation_configuration.maximum_correction_attempts",
                "Correction attempts must remain within the existing one-to-three bound.",
            )
        )
    if config.prompt_version != CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION:
        issues.append(
            _new_issue(
                "unsupported_scenario_configuration",
                "scenario_generation_configuration.prompt_version",
                "Scenario prompt version does not match the existing generator.",
            )
        )
    return tuple(issues)


def validate_market_scenario_orchestration_input(
    orchestration_input: MarketScenarioOrchestrationInput,
    *,
    provider: AnalysisProvider | None,
    scoring_profile: PatternRetrievalScoringProfile = (
        DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
    ),
) -> ValidationResult:
    if not isinstance(
        orchestration_input,
        MarketScenarioOrchestrationInput,
    ):
        raise TypeError(
            "orchestration_input must be MarketScenarioOrchestrationInput."
        )
    issues: list[ValidationIssue] = []
    if (
        orchestration_input.schema_version
        != MARKET_SCENARIO_ORCHESTRATION_INPUT_SCHEMA_VERSION
    ):
        issues.append(
            _new_issue(
                "missing_required_input",
                "schema_version",
                "Orchestration input schema is unsupported.",
            )
        )
    if (
        not orchestration_input.content_hash
        or orchestration_input.content_hash
        != market_scenario_orchestration_input_content_hash(
            orchestration_input
        )
    ):
        issues.append(
            _new_issue(
                "invalid_input_hash",
                "content_hash",
                "Orchestration input hash is missing or mismatched.",
            )
        )
    premise = orchestration_input.frozen_technical_premise
    premise_hash = frozen_technical_premise_content_hash(premise)
    if (
        not orchestration_input.orchestration_input_id.strip()
        or not orchestration_input.symbol.strip()
        or not premise.resolution_id.strip()
        or not premise.source_hashes
    ):
        issues.append(
            _new_issue(
                "missing_required_input",
                "orchestration_input",
                "Input identity, symbol, premise identity, and source hashes are required.",
            )
        )
    if (
        not premise.frozen_content_hash
        or premise.frozen_content_hash != premise_hash
    ):
        issues.append(
            _new_issue(
                "invalid_input_hash",
                "frozen_technical_premise.frozen_content_hash",
                "Frozen technical premise hash is invalid.",
            )
        )
    if premise.symbol != orchestration_input.symbol:
        issues.append(
            _new_issue(
                "missing_required_input",
                "symbol",
                "Input symbol does not match the frozen technical premise.",
            )
        )
    cutoffs = (
        premise.market_data_cutoff,
        orchestration_input.historical_pattern_library.applicable_cutoff,
        *(
            item.applicable_cutoff
            for item in orchestration_input.current_evidence
        ),
        *(
            item.applicable_cutoff
            for item in orchestration_input.current_events
        ),
    )
    final_cutoff = _temporal_point(
        orchestration_input.applicable_cutoff
    )
    if (
        final_cutoff is None
        or any(
            _temporal_point(item) is None
            or _temporal_point(item) > final_cutoff
            for item in cutoffs
        )
    ):
        issues.append(
            _new_issue(
                "cutoff_mismatch",
                "applicable_cutoff",
                "Every source cutoff must be valid and at or before the orchestration cutoff.",
            )
        )

    evidence_ids = tuple(
        item.evidence_id
        for item in orchestration_input.current_evidence
    )
    exposure_ids = tuple(
        item.exposure_id
        for item in orchestration_input.current_exposures
    )
    event_ids = tuple(
        item.event_id
        for item in orchestration_input.current_events
    )
    if (
        _duplicates(evidence_ids)
        or _duplicates(exposure_ids)
        or _duplicates(event_ids)
    ):
        issues.append(
            _new_issue(
                "missing_required_input",
                "current_inputs",
                "Evidence, exposure, and event identities must be unique.",
            )
        )

    library = orchestration_input.historical_pattern_library
    library_validation = validate_historical_pattern_library(library)
    if (
        library.content_hash
        != historical_pattern_library_content_hash(library)
        or not library_validation.is_valid
        or library.validation_result != library_validation
    ):
        issues.append(
            _new_issue(
                "invalid_historical_library",
                "historical_pattern_library",
                "Historical pattern library is not canonically valid.",
            )
        )
    profile_validation = validate_pattern_retrieval_scoring_profile(
        scoring_profile
    )
    if not profile_validation.is_valid:
        issues.append(
            _new_issue(
                "unsupported_retrieval_configuration",
                "scoring_profile",
                "Active pattern-retrieval scoring profile is invalid.",
            )
        )
    issues.extend(
        _validate_retrieval_configuration(
            orchestration_input.retrieval_configuration,
            scoring_profile,
        )
    )
    issues.extend(
        _validate_scenario_configuration(
            orchestration_input.scenario_generation_configuration
        )
    )
    if provider is None or not hasattr(provider, "generate"):
        issues.append(
            _new_issue(
                "missing_required_input",
                "provider",
                "An existing configured provider or offline fixture provider is required.",
            )
        )
    else:
        provider_name = str(
            getattr(provider, "name", "") or "unknown"
        )
        configured_name = (
            orchestration_input.provider_configuration.get(
                "provider_name"
            )
        )
        configured_model = (
            orchestration_input.provider_configuration.get(
                "model_name"
            )
        )
        actual_model = str(
            getattr(provider, "model", None)
            or f"{provider_name}-offline"
        )
        if configured_name and configured_name != provider_name:
            issues.append(
                _new_issue(
                    "missing_required_input",
                    "provider_configuration.provider_name",
                    "Configured provider identity does not match the supplied provider.",
                )
            )
        if configured_model and configured_model != actual_model:
            issues.append(
                _new_issue(
                    "missing_required_input",
                    "provider_configuration.model_name",
                    "Configured model identity does not match the supplied provider.",
                )
            )
        if (
            orchestration_input.execution_mode
            in {
                ExecutionMode.OFFLINE_FIXTURE,
                ExecutionMode.PACKET_REPLAY,
            }
            and provider_name != "fixture"
        ):
            issues.append(
                _new_issue(
                    "unsupported_execution_mode",
                    "execution_mode",
                    "Offline fixture and packet replay modes require a fixture provider.",
                )
            )
    return _validation_from_issues(issues)


def _finalize_stage_record(
    *,
    stage: OrchestrationStage,
    status: OrchestrationStatus,
    started_at: str,
    completed_at: str,
    input_hashes: Mapping[str, str],
    output_hashes: Mapping[str, str],
    validation_result: ValidationResult,
    warnings: Sequence[str] = (),
    errors: Sequence[str] = (),
    attempts: int = 0,
    provider: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
) -> StageExecutionRecord:
    seed = StageExecutionRecord(
        stage=stage,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        input_hashes=input_hashes,
        output_hashes=output_hashes,
        validation_result=validation_result,
        warnings=tuple(warnings),
        errors=tuple(errors),
        attempts=attempts,
        provider=provider,
        model_name=model_name,
        prompt_version=prompt_version,
    )
    seed = replace(
        seed,
        analytical_hash=stage_execution_record_analytical_hash(seed),
    )
    return replace(
        seed,
        content_hash=stage_execution_record_content_hash(seed),
    )


def _duration_ms(started_at: str, completed_at: str) -> int:
    start = _temporal_point(started_at)
    end = _temporal_point(completed_at)
    if start is None or end is None or end < start:
        return 0
    return max(0, int(round((end - start).total_seconds() * 1000)))


def _stage_issue_strings(
    validation: ValidationResult,
) -> tuple[str, ...]:
    return tuple(
        f"{item.code} at {item.path}: {item.message}"
        for item in validation.errors
    )


def _snapshot_input_hashes(
    orchestration_input: MarketScenarioOrchestrationInput,
) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "orchestration_input": (
                market_scenario_orchestration_input_content_hash(
                    orchestration_input
                )
            ),
            "frozen_technical_premise": (
                frozen_technical_premise_content_hash(
                    orchestration_input.frozen_technical_premise
                )
            ),
            "evidence_packet": evidence_packet_content_hash(
                orchestration_input.current_evidence
            ),
            "exposure_report": exposure_report_content_hash(
                orchestration_input.current_exposures,
                orchestration_input.exposure_category_coverage,
            ),
            "material_event_packet": material_event_packet_content_hash(
                orchestration_input.current_events
            ),
            "historical_pattern_library": (
                historical_pattern_library_content_hash(
                    orchestration_input.historical_pattern_library
                )
            ),
            "retrieval_configuration": (
                pattern_retrieval_configuration_content_hash(
                    orchestration_input.retrieval_configuration
                )
            ),
            "scenario_generation_configuration": (
                scenario_generation_configuration_content_hash(
                    orchestration_input.scenario_generation_configuration
                )
            ),
        }
    )


def _validate_pipeline_continuity(
    *,
    orchestration_input: MarketScenarioOrchestrationInput,
    initial_input_hashes: Mapping[str, str],
    current_state: CurrentMarketState | None,
    retrieval: PatternRetrievalResult | None,
    scenario_set: CurrentScenarioSet | None,
    audit_graph: ReasoningAuditGraph | None,
    stage_records: Sequence[StageExecutionRecord],
    stopped_status: OrchestrationStatus,
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    current_hashes = _snapshot_input_hashes(orchestration_input)
    if dict(initial_input_hashes) != dict(current_hashes):
        issues.append(
            _new_issue(
                "immutable_input_violation",
                "orchestration_input",
                "One or more orchestration source inputs changed during execution.",
            )
        )
    stages = tuple(record.stage for record in stage_records)
    if _duplicates(tuple(item.value for item in stages)):
        issues.append(
            _new_issue(
                "duplicate_stage_record",
                "stage_records",
                "A stage appears more than once.",
            )
        )
    if any(
        _STAGE_ORDER[stages[index]]
        >= _STAGE_ORDER[stages[index + 1]]
        for index in range(len(stages) - 1)
    ):
        issues.append(
            _new_issue(
                "stage_order_violation",
                "stage_records",
                "Stage records are not in canonical order.",
            )
        )
    failed_index = next(
        (
            index
            for index, record in enumerate(stage_records)
            if record.stage in _ANALYTICAL_STAGES
            and record.status in _FAILURE_STATUSES
        ),
        None,
    )
    if failed_index is not None and any(
        record.stage in _ANALYTICAL_STAGES
        for record in stage_records[failed_index + 1 :]
    ):
        issues.append(
            _new_issue(
                "downstream_after_failure",
                "stage_records",
                "An analytical stage ran after an upstream terminal status.",
            )
        )
    records_by_stage = {
        record.stage: record for record in stage_records
    }
    if current_state is not None:
        validation = validate_current_market_state(current_state)
        if (
            not validation.is_valid
            or current_state.validation_result != validation
            or current_state.content_hash
            != current_market_state_content_hash(current_state)
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "current_state",
                    "Current state is not canonically valid.",
                )
            )
        if (
            current_state.frozen_technical_premise_hash
            != orchestration_input.frozen_technical_premise.frozen_content_hash
            or current_state.evidence_packet_hash
            != initial_input_hashes["evidence_packet"]
            or current_state.exposure_report_hash
            != initial_input_hashes["exposure_report"]
            or current_state.applicable_cutoff
            != orchestration_input.applicable_cutoff
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "current_state",
                    "Current-state technical, evidence, exposure, or cutoff linkage is inconsistent.",
                )
            )
        state_record = records_by_stage.get(
            OrchestrationStage.CURRENT_STATE_BUILD
        )
        if (
            state_record is None
            or state_record.output_hashes.get("current_state")
            != current_state.content_hash
        ):
            issues.append(
                _new_issue(
                    "immutable_output_violation",
                    "current_state",
                    "Current-state hash changed after its stage record was frozen.",
                )
            )
    if retrieval is not None:
        retrieval_validation = validate_pattern_retrieval_result(
            retrieval,
            state=current_state,
            library=orchestration_input.historical_pattern_library,
        )
        if (
            not retrieval_validation.is_valid
            or retrieval.validation_result != retrieval_validation
            or retrieval.content_hash
            != pattern_retrieval_result_content_hash(retrieval)
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "pattern_retrieval_result",
                    "Pattern retrieval result is not canonically valid.",
                )
            )
        if (
            current_state is None
            or retrieval.query.current_state_hash
            != current_state.content_hash
            or retrieval.query.library_hash
            != orchestration_input.historical_pattern_library.content_hash
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "pattern_retrieval_result.query",
                    "Retrieval query does not preserve state or library hashes.",
                )
            )
        retrieval_record = records_by_stage.get(
            OrchestrationStage.PATTERN_RETRIEVAL
        )
        if (
            retrieval_record is None
            or retrieval_record.output_hashes.get(
                "pattern_retrieval_result"
            )
            != retrieval.content_hash
        ):
            issues.append(
                _new_issue(
                    "immutable_output_violation",
                    "pattern_retrieval_result",
                    "Retrieval hash changed after its stage record was frozen.",
                )
            )
    if scenario_set is not None:
        if current_state is None or retrieval is None:
            issues.append(
                _new_issue(
                    "partial_output_inconsistent",
                    "current_scenario_set",
                    "Scenario output cannot exist without state and retrieval outputs.",
                )
            )
        else:
            scenario_validation = validate_current_scenario_set(
                scenario_set,
                premise=orchestration_input.frozen_technical_premise,
                state=current_state,
                retrieval=retrieval,
                evidence=orchestration_input.current_evidence,
                exposures=orchestration_input.current_exposures,
                events=orchestration_input.current_events,
            )
            if (
                not scenario_validation.is_valid
                or scenario_set.validation_result
                != scenario_validation
            ):
                issues.append(
                    _new_issue(
                        "hash_continuity_failure",
                        "current_scenario_set",
                        "Current scenario set is not canonically valid.",
                    )
                )
            if (
                scenario_set.retrieval_result_hash
                != retrieval.content_hash
                or scenario_set.current_state_hash
                != current_state.content_hash
                or scenario_set.frozen_technical_premise_hash
                != orchestration_input.frozen_technical_premise.frozen_content_hash
                or scenario_set.applicable_cutoff
                != orchestration_input.applicable_cutoff
            ):
                issues.append(
                    _new_issue(
                        "hash_continuity_failure",
                        "current_scenario_set",
                        "Scenario set does not preserve retrieval, state, technical, or cutoff hashes.",
                    )
                )
        scenario_record = records_by_stage.get(
            OrchestrationStage.SCENARIO_GENERATION
        )
        if (
            scenario_record is None
            or scenario_record.output_hashes.get(
                "current_scenario_set"
            )
            != scenario_set.content_hash
        ):
            issues.append(
                _new_issue(
                    "immutable_output_violation",
                    "current_scenario_set",
                    "Scenario-set hash changed after its stage record was frozen.",
                )
            )
    if audit_graph is not None:
        audit_validation = validate_reasoning_audit_graph(
            audit_graph,
            scenario_set=scenario_set,
        )
        if (
            scenario_set is None
            or not audit_validation.is_valid
            or audit_graph.validation_result != audit_validation
            or audit_graph.content_hash
            != reasoning_audit_graph_content_hash(audit_graph)
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "reasoning_audit_graph",
                    "Reasoning-audit graph is not canonically linked.",
                )
            )
    if (
        stopped_status in _SUCCESS_STATUSES
        and scenario_set is None
    ):
        issues.append(
            _new_issue(
                "scenario_set_missing_after_success",
                "current_scenario_set",
                "Successful orchestration requires a scenario set.",
            )
        )
    return _validation_from_issues(issues)


def validate_market_scenario_orchestration_result(
    result: MarketScenarioOrchestrationResult,
) -> ValidationResult:
    if not isinstance(result, MarketScenarioOrchestrationResult):
        raise TypeError(
            "result must be MarketScenarioOrchestrationResult."
        )
    issues: list[ValidationIssue] = []
    if (
        result.schema_version
        != MARKET_SCENARIO_ORCHESTRATION_RESULT_SCHEMA_VERSION
    ):
        issues.append(
            _new_issue(
                "final_content_hash_mismatch",
                "schema_version",
                "Orchestration result schema is unsupported.",
            )
        )
    if (
        not result.content_hash
        or result.content_hash
        != market_scenario_orchestration_result_content_hash(result)
    ):
        issues.append(
            _new_issue(
                "final_content_hash_mismatch",
                "content_hash",
                "Orchestration result content hash is invalid.",
            )
        )
    if (
        not result.analytical_hash
        or result.analytical_hash
        != market_scenario_orchestration_result_analytical_hash(result)
    ):
        issues.append(
            _new_issue(
                "final_content_hash_mismatch",
                "analytical_hash",
                "Orchestration analytical hash is invalid.",
            )
        )
    stages = tuple(record.stage for record in result.stage_records)
    if (
        not stages
        or stages[0] is not OrchestrationStage.INPUT_VALIDATION
        or OrchestrationStage.FINAL_VALIDATION not in stages
        or stages[-1] is not OrchestrationStage.COMPLETED
    ):
        issues.append(
            _new_issue(
                "missing_stage_record",
                "stage_records",
                "Input validation, final validation, and completed stage records are required.",
            )
        )
    if _duplicates(tuple(item.value for item in stages)):
        issues.append(
            _new_issue(
                "duplicate_stage_record",
                "stage_records",
                "Orchestration result contains duplicate stage records.",
            )
        )
    if any(
        _STAGE_ORDER[stages[index]]
        >= _STAGE_ORDER[stages[index + 1]]
        for index in range(len(stages) - 1)
    ):
        issues.append(
            _new_issue(
                "stage_order_violation",
                "stage_records",
                "Orchestration stage order is invalid.",
            )
        )
    for index, record in enumerate(result.stage_records):
        if (
            record.schema_version
            != MARKET_SCENARIO_STAGE_RECORD_SCHEMA_VERSION
            or record.analytical_hash
            != stage_execution_record_analytical_hash(record)
            or record.content_hash
            != stage_execution_record_content_hash(record)
        ):
            issues.append(
                _new_issue(
                    "final_content_hash_mismatch",
                    f"stage_records[{index}]",
                    "Stage record schema or hash is invalid.",
                )
            )
    records_by_stage = {
        record.stage: record for record in result.stage_records
    }
    if result.current_state is not None:
        state_validation = validate_current_market_state(
            result.current_state
        )
        state_record = records_by_stage.get(
            OrchestrationStage.CURRENT_STATE_BUILD
        )
        if (
            not state_validation.is_valid
            or result.current_state.validation_result
            != state_validation
            or result.current_state.content_hash
            != current_market_state_content_hash(
                result.current_state
            )
            or state_record is None
            or state_record.output_hashes.get("current_state")
            != result.current_state.content_hash
            or result.current_state.frozen_technical_premise_hash
            != result.frozen_technical_premise_hash
            or result.current_state.applicable_cutoff
            != result.applicable_cutoff
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "current_state",
                    "Result current-state hash or provenance is inconsistent.",
                )
            )
    if result.state_build_result is not None:
        if (
            result.state_build_result.content_hash
            != current_state_build_result_content_hash(
                result.state_build_result
            )
            or result.state_build_result.current_state
            != result.current_state
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "state_build_result",
                    "State-build wrapper is inconsistent with the retained state.",
                )
            )
    if result.pattern_retrieval_result is not None:
        retrieval_validation = validate_pattern_retrieval_result(
            result.pattern_retrieval_result,
            state=result.current_state,
        )
        retrieval_record = records_by_stage.get(
            OrchestrationStage.PATTERN_RETRIEVAL
        )
        if (
            not retrieval_validation.is_valid
            or result.pattern_retrieval_result.validation_result
            != retrieval_validation
            or result.pattern_retrieval_result.content_hash
            != pattern_retrieval_result_content_hash(
                result.pattern_retrieval_result
            )
            or result.current_state is None
            or result.pattern_retrieval_result.query.current_state_hash
            != result.current_state.content_hash
            or retrieval_record is None
            or retrieval_record.output_hashes.get(
                "pattern_retrieval_result"
            )
            != result.pattern_retrieval_result.content_hash
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "pattern_retrieval_result",
                    "Retained retrieval hash or state linkage is inconsistent.",
                )
            )
    if result.retrieval_execution_result is not None:
        if (
            result.retrieval_execution_result.content_hash
            != pattern_retrieval_execution_result_content_hash(
                result.retrieval_execution_result
            )
            or result.retrieval_execution_result.retrieval_result
            != result.pattern_retrieval_result
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "retrieval_execution_result",
                    "Retrieval wrapper is inconsistent with the retained retrieval.",
                )
            )
    if result.current_scenario_set is not None:
        scenario_record = records_by_stage.get(
            OrchestrationStage.SCENARIO_GENERATION
        )
        if (
            result.current_scenario_set.content_hash
            != current_scenario_set_content_hash(
                result.current_scenario_set
            )
            or result.pattern_retrieval_result is None
            or result.current_state is None
            or result.current_scenario_set.retrieval_result_hash
            != result.pattern_retrieval_result.content_hash
            or result.current_scenario_set.current_state_hash
            != result.current_state.content_hash
            or result.current_scenario_set.frozen_technical_premise_hash
            != result.frozen_technical_premise_hash
            or result.current_scenario_set.applicable_cutoff
            != result.applicable_cutoff
            or scenario_record is None
            or scenario_record.output_hashes.get(
                "current_scenario_set"
            )
            != result.current_scenario_set.content_hash
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "current_scenario_set",
                    "Retained scenario-set hash or provenance is inconsistent.",
                )
            )
    if result.scenario_generation_result is not None:
        generation_validation = (
            validate_current_scenario_generation_result(
                result.scenario_generation_result,
                state=result.current_state,
                retrieval=result.pattern_retrieval_result,
            )
        )
        if (
            not generation_validation.is_valid
            or result.scenario_generation_result.scenario_set
            != result.current_scenario_set
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "scenario_generation_result",
                    "Scenario-generation wrapper is inconsistent with the retained scenario set.",
                )
            )
    if result.reasoning_audit_graph is not None:
        graph_validation = validate_reasoning_audit_graph(
            result.reasoning_audit_graph,
            scenario_set=result.current_scenario_set,
        )
        if (
            not graph_validation.is_valid
            or result.reasoning_audit_graph.validation_result
            != graph_validation
        ):
            issues.append(
                _new_issue(
                    "hash_continuity_failure",
                    "reasoning_audit_graph",
                    "Reasoning-audit graph is inconsistent.",
                )
            )
    if result.audit_coverage_metrics is not None and (
        result.audit_coverage_metrics.content_hash
        != audit_coverage_metrics_content_hash(
            result.audit_coverage_metrics
        )
    ):
        issues.append(
            _new_issue(
                "hash_continuity_failure",
                "audit_coverage_metrics",
                "Audit-coverage metrics hash is inconsistent.",
            )
        )
    failed_index = next(
        (
            index
            for index, record in enumerate(result.stage_records)
            if record.stage in _ANALYTICAL_STAGES
            and record.status in _FAILURE_STATUSES
        ),
        None,
    )
    if failed_index is not None and any(
        record.stage in _ANALYTICAL_STAGES
        for record in result.stage_records[failed_index + 1 :]
    ):
        issues.append(
            _new_issue(
                "downstream_after_failure",
                "stage_records",
                "A downstream analytical stage followed an upstream failure.",
            )
        )
    if result.success != (result.status in _SUCCESS_STATUSES):
        issues.append(
            _new_issue(
                "inconsistent_success_status",
                "success",
                "Success flag and terminal status disagree.",
            )
        )
    if result.status not in _SUCCESS_STATUSES | _FAILURE_STATUSES:
        issues.append(
            _new_issue(
                "invalid_final_status",
                "status",
                "Terminal orchestration status is invalid.",
            )
        )
    start = _temporal_point(result.started_at)
    end = _temporal_point(result.completed_at)
    expected_duration = _duration_ms(
        result.started_at,
        result.completed_at,
    )
    if (
        start is None
        or end is None
        or end < start
        or result.duration_ms != expected_duration
    ):
        issues.append(
            _new_issue(
                "invalid_duration",
                "duration_ms",
                "Orchestration duration or timestamps are inconsistent.",
            )
        )
    if any(
        record.completed_at < record.started_at
        for record in result.stage_records
    ):
        issues.append(
            _new_issue(
                "invalid_duration",
                "stage_records",
                "A stage completion precedes its start.",
            )
        )
    if (
        result.current_state is None
        and result.pattern_retrieval_result is not None
    ) or (
        result.pattern_retrieval_result is None
        and result.current_scenario_set is not None
    ):
        issues.append(
            _new_issue(
                "partial_output_inconsistent",
                "current_state",
                "Partial outputs violate stage dependency order.",
            )
        )
    final_stage = next(
        (
            record
            for record in result.stage_records
            if record.stage is OrchestrationStage.FINAL_VALIDATION
        ),
        None,
    )
    if final_stage is not None and not final_stage.validation_result.is_valid:
        issues.extend(final_stage.validation_result.errors)
    if result.success and result.current_scenario_set is None:
        issues.append(
            _new_issue(
                "scenario_set_missing_after_success",
                "current_scenario_set",
                "Successful scenario generation requires an output set.",
            )
        )
    return _validation_from_issues(issues)


def _finalize_orchestration_result(
    seed: MarketScenarioOrchestrationResult,
) -> MarketScenarioOrchestrationResult:
    first = replace(
        seed,
        final_validation_result=ValidationResult.unvalidated(),
        analytical_hash="",
        content_hash="",
    )
    first = replace(
        first,
        analytical_hash=(
            market_scenario_orchestration_result_analytical_hash(first)
        ),
    )
    first = replace(
        first,
        content_hash=market_scenario_orchestration_result_content_hash(
            first
        ),
    )
    validation = validate_market_scenario_orchestration_result(first)
    finalized = replace(
        first,
        final_validation_result=validation,
        analytical_hash="",
        content_hash="",
    )
    finalized = replace(
        finalized,
        analytical_hash=(
            market_scenario_orchestration_result_analytical_hash(
                finalized
            )
        ),
    )
    finalized = replace(
        finalized,
        content_hash=market_scenario_orchestration_result_content_hash(
            finalized
        ),
    )
    repeated = validate_market_scenario_orchestration_result(finalized)
    if repeated != validation:
        raise RuntimeError(
            "Market-scenario orchestration validation did not stabilize."
        )
    return finalized


class _ReplayFixtureProvider:
    name = "fixture"

    def __init__(
        self,
        responses: Sequence[Mapping[str, Any]],
        *,
        model: str,
    ) -> None:
        self.model = model
        self.responses = [
            copy.deepcopy(_json_value(item)) for item in responses
        ]
        self.calls = 0
        self.response_mismatch = False

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        del system_prompt, user_prompt, schema, packet
        self.calls += 1
        if not self.responses:
            self.response_mismatch = True
            raise RuntimeError(
                "fixture_response_mismatch: replay fixture response queue is empty."
            )
        value = self.responses.pop(0)
        if not isinstance(value, dict):
            raise RuntimeError(
                "fixture_response_mismatch: response is not an object."
            )
        return copy.deepcopy(value)

    def generate_strict_json(self, **values: Any) -> dict[str, Any]:
        return self.generate(**values)


class MarketScenarioOrchestrator:
    """Coordinate Phase 6 and Phase 7 services over frozen inputs."""

    def __init__(
        self,
        provider: AnalysisProvider,
        *,
        scoring_profile: PatternRetrievalScoringProfile = (
            DEFAULT_PATTERN_RETRIEVAL_SCORING_PROFILE
        ),
        max_prompt_characters: int = 180_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(provider, "generate"):
            raise TypeError(
                "provider must implement AnalysisProvider.generate()."
            )
        if not isinstance(
            scoring_profile,
            PatternRetrievalScoringProfile,
        ):
            raise TypeError(
                "scoring_profile must be PatternRetrievalScoringProfile."
            )
        self.provider = provider
        self.scoring_profile = scoring_profile
        self.max_prompt_characters = max_prompt_characters
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def provider_name(self) -> str:
        return str(
            getattr(self.provider, "name", "") or "unknown"
        )

    @property
    def model_name(self) -> str:
        return str(
            getattr(self.provider, "model", None)
            or f"{self.provider_name}-offline"
        )

    def _now(self) -> str:
        return _normalize_timestamp(
            self.clock(),
            field_name="operational_timestamp",
        )

    def execute(
        self,
        orchestration_input: MarketScenarioOrchestrationInput,
    ) -> MarketScenarioOrchestrationResult:
        if not isinstance(
            orchestration_input,
            MarketScenarioOrchestrationInput,
        ):
            raise TypeError(
                "orchestration_input must be MarketScenarioOrchestrationInput."
            )
        overall_started = self._now()
        logical_time = _temporal_point(orchestration_input.created_at)
        if logical_time is None:
            logical_time = datetime.now(timezone.utc)
        logical_clock = lambda: logical_time
        stage_records: list[StageExecutionRecord] = []
        warnings: list[str] = []
        errors: list[str] = []
        state_result: CurrentStateBuildResult | None = None
        retrieval_execution: (
            PatternRetrievalExecutionResult | None
        ) = None
        scenario_generation: (
            CurrentScenarioGenerationResult | None
        ) = None
        current_state: CurrentMarketState | None = None
        retrieval: PatternRetrievalResult | None = None
        scenario_set: CurrentScenarioSet | None = None
        audit_graph: ReasoningAuditGraph | None = None
        audit_metrics: AuditCoverageMetrics | None = None
        terminal_status = OrchestrationStatus.FAILED
        input_hashes = _snapshot_input_hashes(orchestration_input)
        orchestration_id = "orchestration_" + _hash_payload(
            {
                "input_hash": orchestration_input.content_hash,
                "orchestrator_version": (
                    MARKET_SCENARIO_ORCHESTRATOR_VERSION
                ),
            }
        )[:20]

        input_started = self._now()
        input_validation = (
            validate_market_scenario_orchestration_input(
                orchestration_input,
                provider=self.provider,
                scoring_profile=self.scoring_profile,
            )
        )
        input_completed = self._now()
        input_status = (
            OrchestrationStatus.COMPLETED
            if input_validation.is_valid
            else OrchestrationStatus.FAILED
        )
        stage_records.append(
            _finalize_stage_record(
                stage=OrchestrationStage.INPUT_VALIDATION,
                status=input_status,
                started_at=input_started,
                completed_at=input_completed,
                input_hashes=input_hashes,
                output_hashes={
                    "validation": _hash_payload(
                        input_validation.to_dict()
                    )
                },
                validation_result=input_validation,
                warnings=tuple(
                    item.message
                    for item in input_validation.warnings
                ),
                errors=_stage_issue_strings(input_validation),
            )
        )
        if not input_validation.is_valid:
            errors.extend(_stage_issue_strings(input_validation))
        else:
            state_started = self._now()
            try:
                state_result = CurrentStateBuilder(
                    clock=logical_clock
                ).build(
                    orchestration_input.frozen_technical_premise,
                    orchestration_input.current_evidence,
                    orchestration_input.current_exposures,
                    orchestration_input.exposure_category_coverage,
                    events=orchestration_input.current_events,
                    market_regime=(
                        orchestration_input.current_regime_inputs
                    ),
                    valuation_state=(
                        orchestration_input.valuation_inputs
                    ),
                    liquidity_state=(
                        orchestration_input.liquidity_inputs
                    ),
                    positioning_state=(
                        orchestration_input.positioning_inputs
                    ),
                    benchmark_relative_state=(
                        orchestration_input.benchmark_relative_inputs
                    ),
                    sector_relative_state=(
                        orchestration_input.sector_relative_inputs
                    ),
                    availability_overrides=(
                        orchestration_input.availability_overrides
                    ),
                    assumptions=orchestration_input.state_assumptions,
                    applicable_cutoff=(
                        orchestration_input.applicable_cutoff
                    ),
                    as_of=orchestration_input.created_at,
                )
                state_result_integrity = (
                    state_result.content_hash
                    == current_state_build_result_content_hash(
                        state_result
                    )
                    and state_result.output_hash
                    == (
                        state_result.current_state.content_hash
                        if state_result.current_state is not None
                        else ""
                    )
                    and (
                        not state_result.success
                        or (
                            state_result.current_state is not None
                            and state_result.validation_result.is_valid
                        )
                    )
                )
                if not state_result_integrity:
                    state_result = None
                    raise RuntimeError(
                        "Current-state execution result failed its hash or output contract."
                    )
                state_completed = self._now()
                current_state = state_result.current_state
                state_status = (
                    OrchestrationStatus.COMPLETED_WITH_WARNINGS
                    if state_result.success
                    and state_result.warnings
                    else (
                        OrchestrationStatus.COMPLETED
                        if state_result.success
                        else OrchestrationStatus.FAILED
                    )
                )
                stage_records.append(
                    _finalize_stage_record(
                        stage=OrchestrationStage.CURRENT_STATE_BUILD,
                        status=state_status,
                        started_at=state_started,
                        completed_at=state_completed,
                        input_hashes=input_hashes,
                        output_hashes={
                            "state_build_result": (
                                state_result.content_hash
                            ),
                            **(
                                {
                                    "current_state": (
                                        current_state.content_hash
                                    )
                                }
                                if current_state is not None
                                else {}
                            ),
                        },
                        validation_result=(
                            state_result.validation_result
                        ),
                        warnings=state_result.warnings,
                        errors=state_result.errors,
                    )
                )
                warnings.extend(state_result.warnings)
                errors.extend(state_result.errors)
            except Exception as exc:
                state_completed = self._now()
                state_validation = _validation_from_issues(
                    (
                        _new_issue(
                            "current_state_stage_exception",
                            "current_state_build",
                            f"Current-state stage failed: {exc}",
                        ),
                    )
                )
                stage_records.append(
                    _finalize_stage_record(
                        stage=OrchestrationStage.CURRENT_STATE_BUILD,
                        status=OrchestrationStatus.FAILED,
                        started_at=state_started,
                        completed_at=state_completed,
                        input_hashes=input_hashes,
                        output_hashes={},
                        validation_result=state_validation,
                        errors=_stage_issue_strings(
                            state_validation
                        ),
                    )
                )
                errors.extend(_stage_issue_strings(state_validation))

            if state_result is not None and state_result.success:
                retrieval_started = self._now()
                try:
                    retrieval_config = (
                        orchestration_input.retrieval_configuration
                    )
                    query = create_pattern_retrieval_query(
                        current_state,
                        orchestration_input.historical_pattern_library,
                        requested_limit=(
                            retrieval_config.requested_limit
                        ),
                        requested_counter_limit=(
                            retrieval_config.counter_pattern_limit
                        ),
                        minimum_match_quality=(
                            retrieval_config.minimum_match_quality
                        ),
                        include_context_dependent_patterns=(
                            retrieval_config.include_context_dependent_patterns
                        ),
                        include_bidirectional_patterns=(
                            retrieval_config.include_bidirectional_patterns
                        ),
                        allowed_pattern_statuses=(
                            retrieval_config.allowed_pattern_statuses
                        ),
                        scoring_profile=self.scoring_profile,
                        generated_at=orchestration_input.created_at,
                    )
                    retrieval_execution = PatternRetrievalEngine(
                        scoring_profile=self.scoring_profile,
                        clock=logical_clock,
                    ).retrieve(
                        current_state,
                        orchestration_input.historical_pattern_library,
                        query,
                    )
                    retrieval_execution_integrity = (
                        retrieval_execution.content_hash
                        == pattern_retrieval_execution_result_content_hash(
                            retrieval_execution
                        )
                        and retrieval_execution.state_hash
                        == current_state.content_hash
                        and retrieval_execution.library_hash
                        == orchestration_input.historical_pattern_library.content_hash
                        and (
                            not retrieval_execution.success
                            or (
                                retrieval_execution.retrieval_result
                                is not None
                                and retrieval_execution.validation_result.is_valid
                            )
                        )
                    )
                    if not retrieval_execution_integrity:
                        retrieval_execution = None
                        raise RuntimeError(
                            "Pattern-retrieval execution result failed its hash or linkage contract."
                        )
                    retrieval_completed = self._now()
                    retrieval = (
                        retrieval_execution.retrieval_result
                    )
                    no_matches = (
                        retrieval_execution.success
                        and retrieval is not None
                        and not retrieval.matches
                    )
                    if no_matches:
                        retrieval_status = (
                            OrchestrationStatus.INSUFFICIENT_EVIDENCE
                        )
                    elif retrieval_execution.success:
                        retrieval_status = (
                            OrchestrationStatus.COMPLETED_WITH_WARNINGS
                            if retrieval_execution.warnings
                            else OrchestrationStatus.COMPLETED
                        )
                    else:
                        retrieval_status = (
                            OrchestrationStatus.FAILED
                        )
                    stage_records.append(
                        _finalize_stage_record(
                            stage=OrchestrationStage.PATTERN_RETRIEVAL,
                            status=retrieval_status,
                            started_at=retrieval_started,
                            completed_at=retrieval_completed,
                            input_hashes={
                                "current_state": (
                                    current_state.content_hash
                                ),
                                "historical_pattern_library": (
                                    orchestration_input.historical_pattern_library.content_hash
                                ),
                                "retrieval_configuration": (
                                    retrieval_config.content_hash
                                ),
                            },
                            output_hashes={
                                "retrieval_execution_result": (
                                    retrieval_execution.content_hash
                                ),
                                **(
                                    {
                                        "pattern_retrieval_result": (
                                            retrieval.content_hash
                                        )
                                    }
                                    if retrieval is not None
                                    else {}
                                ),
                            },
                            validation_result=(
                                retrieval_execution.validation_result
                            ),
                            warnings=(
                                retrieval_execution.warnings
                                + (
                                    (
                                        "No eligible primary historical pattern matched; scenario generation was not called.",
                                    )
                                    if no_matches
                                    else ()
                                )
                            ),
                            errors=retrieval_execution.errors,
                        )
                    )
                    warnings.extend(retrieval_execution.warnings)
                    errors.extend(retrieval_execution.errors)
                    if no_matches:
                        terminal_status = (
                            OrchestrationStatus.INSUFFICIENT_EVIDENCE
                        )
                        warnings.append(
                            "No eligible primary historical pattern matched; scenario generation was not called."
                        )
                except Exception as exc:
                    retrieval_completed = self._now()
                    retrieval_validation = _validation_from_issues(
                        (
                            _new_issue(
                                "pattern_retrieval_stage_exception",
                                "pattern_retrieval",
                                f"Pattern-retrieval stage failed: {exc}",
                            ),
                        )
                    )
                    stage_records.append(
                        _finalize_stage_record(
                            stage=OrchestrationStage.PATTERN_RETRIEVAL,
                            status=OrchestrationStatus.FAILED,
                            started_at=retrieval_started,
                            completed_at=retrieval_completed,
                            input_hashes={
                                "current_state": (
                                    current_state.content_hash
                                ),
                                "historical_pattern_library": (
                                    orchestration_input.historical_pattern_library.content_hash
                                ),
                            },
                            output_hashes={},
                            validation_result=retrieval_validation,
                            errors=_stage_issue_strings(
                                retrieval_validation
                            ),
                        )
                    )
                    errors.extend(
                        _stage_issue_strings(retrieval_validation)
                    )

                if (
                    retrieval_execution is not None
                    and retrieval_execution.success
                    and retrieval is not None
                    and retrieval.matches
                ):
                    scenario_started = self._now()
                    scenario_config = (
                        orchestration_input.scenario_generation_configuration
                    )
                    try:
                        generator = CurrentMarketScenarioGenerator(
                            self.provider,
                            max_attempts=(
                                scenario_config.maximum_correction_attempts
                            ),
                            use_adversarial_pass=(
                                scenario_config.require_adversarial_pass
                            ),
                            max_prompt_characters=(
                                self.max_prompt_characters
                            ),
                            clock=logical_clock,
                        )
                        scenario_generation = generator.generate(
                            orchestration_input.frozen_technical_premise,
                            current_state,
                            retrieval,
                            orchestration_input.current_evidence,
                            orchestration_input.current_exposures,
                            orchestration_input.current_events,
                        )
                        generation_validation = (
                            validate_current_scenario_generation_result(
                                scenario_generation,
                                premise=(
                                    orchestration_input.frozen_technical_premise
                                ),
                                state=current_state,
                                retrieval=retrieval,
                            )
                        )
                        if not generation_validation.is_valid:
                            raise RuntimeError(
                                "Scenario-generation execution result failed its canonical contract."
                            )
                        scenario_completed = self._now()
                        scenario_set = (
                            scenario_generation.scenario_set
                        )
                        insufficient = (
                            not scenario_generation.success
                            and (
                                scenario_generation.adversarial_recommendation
                                is ScenarioAdversarialRecommendation.INSUFFICIENT_EVIDENCE
                                or any(
                                    "insufficient" in item.casefold()
                                    for item in (
                                        scenario_generation.errors
                                        + scenario_generation.warnings
                                    )
                                )
                            )
                        )
                        scenario_status = (
                            OrchestrationStatus.INSUFFICIENT_EVIDENCE
                            if insufficient
                            else (
                                OrchestrationStatus.COMPLETED_WITH_WARNINGS
                                if scenario_generation.success
                                and scenario_generation.warnings
                                else (
                                    OrchestrationStatus.COMPLETED
                                    if scenario_generation.success
                                    else OrchestrationStatus.FAILED
                                )
                            )
                        )
                        stage_records.append(
                            _finalize_stage_record(
                                stage=OrchestrationStage.SCENARIO_GENERATION,
                                status=scenario_status,
                                started_at=scenario_started,
                                completed_at=scenario_completed,
                                input_hashes={
                                    "frozen_technical_premise": (
                                        orchestration_input.frozen_technical_premise.frozen_content_hash
                                    ),
                                    "current_state": (
                                        current_state.content_hash
                                    ),
                                    "pattern_retrieval_result": (
                                        retrieval.content_hash
                                    ),
                                    "scenario_generation_configuration": (
                                        scenario_config.content_hash
                                    ),
                                },
                                output_hashes={
                                    "scenario_generation_result": (
                                        scenario_generation.content_hash
                                    ),
                                    **(
                                        {
                                            "current_scenario_set": (
                                                scenario_set.content_hash
                                            )
                                        }
                                        if scenario_set is not None
                                        else {}
                                    ),
                                },
                                validation_result=(
                                    scenario_generation.validation_result
                                ),
                                warnings=(
                                    scenario_generation.warnings
                                ),
                                errors=scenario_generation.errors,
                                attempts=scenario_generation.attempts,
                                provider=(
                                    scenario_generation.provider
                                ),
                                model_name=(
                                    scenario_generation.model_name
                                ),
                                prompt_version=(
                                    scenario_generation.prompt_version
                                ),
                            )
                        )
                        warnings.extend(
                            scenario_generation.warnings
                        )
                        errors.extend(scenario_generation.errors)
                        terminal_status = scenario_status
                    except Exception as exc:
                        scenario_completed = self._now()
                        scenario_validation = (
                            _validation_from_issues(
                                (
                                    _new_issue(
                                        "scenario_generation_stage_exception",
                                        "scenario_generation",
                                        f"Scenario-generation stage failed: {exc}",
                                    ),
                                )
                            )
                        )
                        stage_records.append(
                            _finalize_stage_record(
                                stage=OrchestrationStage.SCENARIO_GENERATION,
                                status=OrchestrationStatus.FAILED,
                                started_at=scenario_started,
                                completed_at=scenario_completed,
                                input_hashes={
                                    "frozen_technical_premise": (
                                        orchestration_input.frozen_technical_premise.frozen_content_hash
                                    ),
                                    "current_state": (
                                        current_state.content_hash
                                    ),
                                    "pattern_retrieval_result": (
                                        retrieval.content_hash
                                    ),
                                },
                                output_hashes={},
                                validation_result=(
                                    scenario_validation
                                ),
                                errors=_stage_issue_strings(
                                    scenario_validation
                                ),
                                provider=self.provider_name,
                                model_name=self.model_name,
                                prompt_version=(
                                    CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION
                                ),
                            )
                        )
                        errors.extend(
                            _stage_issue_strings(
                                scenario_validation
                            )
                        )
                        terminal_status = OrchestrationStatus.FAILED
            elif state_result is not None:
                terminal_status = OrchestrationStatus.FAILED

        if (
            scenario_generation is not None
            and scenario_generation.success
            and scenario_set is not None
        ):
            try:
                audit_graph, audit_metrics = (
                    build_reasoning_audit_graph(
                        orchestration_id=orchestration_id,
                        premise=(
                            orchestration_input.frozen_technical_premise
                        ),
                        state=current_state,
                        retrieval=retrieval,
                        scenario_set=scenario_set,
                        evidence=orchestration_input.current_evidence,
                        exposures=(
                            orchestration_input.current_exposures
                        ),
                        events=orchestration_input.current_events,
                        generated_at=orchestration_input.created_at,
                    )
                )
            except Exception as exc:
                terminal_status = OrchestrationStatus.FAILED
                errors.append(
                    f"reasoning_audit_failure: {exc}"
                )

        final_started = self._now()
        continuity_validation = _validate_pipeline_continuity(
            orchestration_input=orchestration_input,
            initial_input_hashes=input_hashes,
            current_state=current_state,
            retrieval=retrieval,
            scenario_set=scenario_set,
            audit_graph=audit_graph,
            stage_records=stage_records,
            stopped_status=terminal_status,
        )
        if not continuity_validation.is_valid:
            terminal_status = OrchestrationStatus.FAILED
            errors.extend(_stage_issue_strings(continuity_validation))
        elif (
            scenario_generation is not None
            and scenario_generation.success
            and scenario_set is not None
        ):
            terminal_status = (
                OrchestrationStatus.COMPLETED_WITH_WARNINGS
                if warnings
                else OrchestrationStatus.COMPLETED
            )
        elif terminal_status not in {
            OrchestrationStatus.INSUFFICIENT_EVIDENCE,
            OrchestrationStatus.FAILED,
        }:
            terminal_status = OrchestrationStatus.FAILED
        final_completed = self._now()
        stage_records.append(
            _finalize_stage_record(
                stage=OrchestrationStage.FINAL_VALIDATION,
                status=(
                    OrchestrationStatus.COMPLETED
                    if continuity_validation.is_valid
                    else OrchestrationStatus.FAILED
                ),
                started_at=final_started,
                completed_at=final_completed,
                input_hashes={
                    **(
                        {
                            "current_state": current_state.content_hash
                        }
                        if current_state is not None
                        else {}
                    ),
                    **(
                        {
                            "pattern_retrieval_result": (
                                retrieval.content_hash
                            )
                        }
                        if retrieval is not None
                        else {}
                    ),
                    **(
                        {
                            "current_scenario_set": (
                                scenario_set.content_hash
                            )
                        }
                        if scenario_set is not None
                        else {}
                    ),
                },
                output_hashes={
                    "continuity_validation": _hash_payload(
                        continuity_validation.to_dict()
                    ),
                    **(
                        {
                            "reasoning_audit_graph": (
                                audit_graph.content_hash
                            ),
                            "audit_coverage_metrics": (
                                audit_metrics.content_hash
                            ),
                        }
                        if audit_graph is not None
                        and audit_metrics is not None
                        else {}
                    ),
                },
                validation_result=continuity_validation,
                errors=_stage_issue_strings(continuity_validation),
            )
        )
        completed_started = self._now()
        completed_at = self._now()
        stage_records.append(
            _finalize_stage_record(
                stage=OrchestrationStage.COMPLETED,
                status=terminal_status,
                started_at=completed_started,
                completed_at=completed_at,
                input_hashes={
                    "orchestration_input": (
                        orchestration_input.content_hash
                    )
                },
                output_hashes={
                    "terminal_analytical_outputs": _hash_payload(
                        {
                            "state": (
                                current_state.content_hash
                                if current_state is not None
                                else None
                            ),
                            "retrieval": (
                                retrieval.content_hash
                                if retrieval is not None
                                else None
                            ),
                            "scenario_set": (
                                scenario_set.content_hash
                                if scenario_set is not None
                                else None
                            ),
                            "audit_graph": (
                                audit_graph.content_hash
                                if audit_graph is not None
                                else None
                            ),
                            "status": terminal_status.value,
                        }
                    )
                },
                validation_result=continuity_validation,
                warnings=tuple(sorted(set(warnings))),
                errors=tuple(sorted(set(errors))),
                provider=self.provider_name,
                model_name=self.model_name,
                prompt_version=(
                    CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION
                ),
            )
        )

        result_seed = MarketScenarioOrchestrationResult(
            orchestration_id=orchestration_id,
            success=terminal_status in _SUCCESS_STATUSES,
            status=terminal_status,
            symbol=orchestration_input.symbol,
            applicable_cutoff=orchestration_input.applicable_cutoff,
            orchestration_input_hash=orchestration_input.content_hash,
            frozen_technical_premise_hash=(
                orchestration_input.frozen_technical_premise.frozen_content_hash
            ),
            current_state=current_state,
            pattern_retrieval_result=retrieval,
            current_scenario_set=scenario_set,
            state_build_result=state_result,
            retrieval_execution_result=retrieval_execution,
            scenario_generation_result=scenario_generation,
            reasoning_audit_graph=audit_graph,
            audit_coverage_metrics=audit_metrics,
            stage_records=tuple(stage_records),
            final_validation_result=ValidationResult.unvalidated(),
            warnings=tuple(sorted(set(warnings))),
            errors=tuple(sorted(set(errors))),
            started_at=overall_started,
            completed_at=completed_at,
            duration_ms=_duration_ms(
                overall_started,
                completed_at,
            ),
            execution_mode=orchestration_input.execution_mode,
            provider=self.provider_name,
            model_name=self.model_name,
            prompt_versions={
                "scenario_synthesis": (
                    CURRENT_SCENARIO_SYNTHESIS_PROMPT_VERSION
                ),
                "scenario_adversarial": (
                    CURRENT_SCENARIO_ADVERSARIAL_PROMPT_VERSION
                ),
            },
        )
        return _finalize_orchestration_result(result_seed)


def validate_market_scenario_replay_packet(
    packet: MarketScenarioReplayPacket,
) -> ValidationResult:
    if not isinstance(packet, MarketScenarioReplayPacket):
        raise TypeError(
            "packet must be MarketScenarioReplayPacket."
        )
    issues: list[ValidationIssue] = []
    if (
        packet.packet_schema_version
        != MARKET_SCENARIO_REPLAY_PACKET_SCHEMA_VERSION
    ):
        issues.append(
            _new_issue(
                "replay_hash_mismatch",
                "packet_schema_version",
                "Replay packet schema version is unsupported.",
            )
        )
    if (
        not packet.content_hash
        or packet.content_hash
        != market_scenario_replay_packet_content_hash(packet)
    ):
        issues.append(
            _new_issue(
                "replay_hash_mismatch",
                "content_hash",
                "Replay packet content hash is invalid.",
            )
        )
    if (
        packet.expected_input_hash
        != packet.orchestration_input.content_hash
        or packet.orchestration_input.content_hash
        != market_scenario_orchestration_input_content_hash(
            packet.orchestration_input
        )
    ):
        issues.append(
            _new_issue(
                "replay_hash_mismatch",
                "expected_input_hash",
                "Replay packet input hash is inconsistent.",
            )
        )
    if (
        packet.orchestration_input.execution_mode
        is not ExecutionMode.PACKET_REPLAY
    ):
        issues.append(
            _new_issue(
                "unsupported_execution_mode",
                "orchestration_input.execution_mode",
                "Replay packets require packet_replay execution mode.",
            )
        )
    if not packet.fixture_provider_responses:
        issues.append(
            _new_issue(
                "fixture_response_mismatch",
                "fixture_provider_responses",
                "Replay packet requires explicit offline fixture responses.",
            )
        )
    return _validation_from_issues(
        issues,
        validation_version=MARKET_SCENARIO_REPLAY_VALIDATION_VERSION,
    )


def replay_market_scenario_packet(
    packet: MarketScenarioReplayPacket,
    *,
    clock: Callable[[], datetime] | None = None,
    max_prompt_characters: int = 180_000,
) -> MarketScenarioReplayResult:
    validation = validate_market_scenario_replay_packet(packet)
    orchestration_result: MarketScenarioOrchestrationResult | None = None
    provider: _ReplayFixtureProvider | None = None
    mismatches: list[str] = []
    warnings: list[str] = []
    actual_stage_hashes: dict[str, str] = {}
    actual_final_hash: str | None = None
    if validation.is_valid:
        provider = _ReplayFixtureProvider(
            packet.fixture_provider_responses,
            model=packet.fixture_model_name,
        )
        orchestration_result = MarketScenarioOrchestrator(
            provider,
            clock=clock,
            max_prompt_characters=max_prompt_characters,
        ).execute(packet.orchestration_input)
        actual_stage_hashes = {
            record.stage.value: record.analytical_hash
            for record in orchestration_result.stage_records
        }
        actual_final_hash = orchestration_result.analytical_hash
        for key, expected in packet.expected_stage_hashes.items():
            if actual_stage_hashes.get(key) != expected:
                mismatches.append(
                    f"stage_hash_mismatch:{key}"
                )
        if (
            packet.expected_final_hash is not None
            and actual_final_hash != packet.expected_final_hash
        ):
            mismatches.append("final_hash_mismatch")
        if provider.responses:
            mismatches.append("unused_fixture_responses")
        if provider.response_mismatch:
            mismatches.append("fixture_response_mismatch")
        if not orchestration_result.final_validation_result.is_valid:
            mismatches.append("orchestration_validation_failed")
    else:
        mismatches.extend(validation.failed_rules)

    replay_issues = list(validation.errors)
    replay_issues.extend(
        _new_issue(
            (
                "fixture_response_mismatch"
                if code
                in {
                    "unused_fixture_responses",
                    "fixture_response_mismatch",
                }
                else "replay_hash_mismatch"
            ),
            "replay",
            code,
        )
        for code in mismatches
        if code not in validation.failed_rules
    )
    final_validation = _validation_from_issues(
        replay_issues,
        validation_version=MARKET_SCENARIO_REPLAY_VALIDATION_VERSION,
    )
    seed = MarketScenarioReplayResult(
        packet_id=packet.packet_id,
        success=(
            final_validation.is_valid
            and orchestration_result is not None
            and orchestration_result.success
        ),
        orchestration_result=orchestration_result,
        validation_result=final_validation,
        expected_input_hash=packet.expected_input_hash,
        actual_input_hash=(
            packet.orchestration_input.content_hash
        ),
        expected_stage_hashes=packet.expected_stage_hashes,
        actual_stage_hashes=actual_stage_hashes,
        expected_final_hash=packet.expected_final_hash,
        actual_final_hash=actual_final_hash,
        mismatch_codes=tuple(sorted(set(mismatches))),
        warnings=tuple(warnings),
        remaining_fixture_responses=(
            len(provider.responses) if provider is not None else 0
        ),
    )
    return replace(
        seed,
        content_hash=market_scenario_replay_result_content_hash(seed),
    )


__all__ = [
    "ExecutionMode",
    "MARKET_SCENARIO_ORCHESTRATION_INPUT_SCHEMA_VERSION",
    "MARKET_SCENARIO_ORCHESTRATION_RESULT_SCHEMA_VERSION",
    "MARKET_SCENARIO_ORCHESTRATION_VALIDATION_VERSION",
    "MARKET_SCENARIO_ORCHESTRATOR_VERSION",
    "MARKET_SCENARIO_REPLAY_PACKET_SCHEMA_VERSION",
    "MARKET_SCENARIO_REPLAY_RESULT_SCHEMA_VERSION",
    "MARKET_SCENARIO_REPLAY_VALIDATION_VERSION",
    "MARKET_SCENARIO_STAGE_RECORD_SCHEMA_VERSION",
    "MarketScenarioOrchestrationInput",
    "MarketScenarioOrchestrationResult",
    "MarketScenarioOrchestrator",
    "MarketScenarioReplayPacket",
    "MarketScenarioReplayResult",
    "OrchestrationStage",
    "OrchestrationStatus",
    "PATTERN_RETRIEVAL_CONFIGURATION_SCHEMA_VERSION",
    "PatternRetrievalConfiguration",
    "SCENARIO_GENERATION_CONFIGURATION_SCHEMA_VERSION",
    "ScenarioGenerationConfiguration",
    "StageExecutionRecord",
    "market_scenario_orchestration_input_content_hash",
    "market_scenario_orchestration_result_analytical_hash",
    "market_scenario_orchestration_result_content_hash",
    "market_scenario_replay_packet_content_hash",
    "market_scenario_replay_result_content_hash",
    "pattern_retrieval_configuration_content_hash",
    "replay_market_scenario_packet",
    "scenario_generation_configuration_content_hash",
    "stage_execution_record_analytical_hash",
    "stage_execution_record_content_hash",
    "validate_market_scenario_orchestration_input",
    "validate_market_scenario_orchestration_result",
    "validate_market_scenario_replay_packet",
]
