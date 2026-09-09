"""Standalone Phase 10 bridge over completed Phase 8 artifacts.

This module is intentionally not called by the active orchestrator. It reuses
the normal current-scenario generator only when explicitly invoked with a
validated company context and a completed immutable orchestration result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any, Mapping

from .company_knowledge import (
    CompanyScenarioContext,
    validate_company_scenario_context,
)
from .current_scenario_generator import CurrentMarketScenarioGenerator
from .current_scenarios import (
    CurrentScenarioGenerationResult,
    validate_current_scenario_generation_result,
)
from .market_scenario import ValidationIssue, ValidationResult
from .market_scenario_orchestrator import (
    MarketScenarioOrchestrationInput,
    MarketScenarioOrchestrationResult,
    market_scenario_orchestration_input_content_hash,
    market_scenario_orchestration_result_content_hash,
)


COMPANY_ORCHESTRATION_BRIDGE_SCHEMA_VERSION = (
    "company-orchestration-bridge-1.0.0"
)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    return value


def _contract_hash(value: Any) -> str:
    payload = {
        item.name: _json_value(getattr(value, item.name))
        for item in fields(value)
        if item.name != "content_hash"
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompanyOrchestrationBridgeResult:
    source_orchestration_input_hash: str
    source_orchestration_result_hash: str
    company_context_hash: str
    scenario_generation_result: CurrentScenarioGenerationResult | None
    validation_result: ValidationResult
    errors: tuple[str, ...]
    content_hash: str = ""
    schema_version: str = COMPANY_ORCHESTRATION_BRIDGE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "CompanyOrchestrationBridgeResult":
        if not isinstance(value, Mapping):
            raise TypeError("from_dict requires a mapping.")
        raw = dict(value)
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "CompanyOrchestrationBridgeResult contains unknown fields: "
                + ", ".join(unknown)
                + "."
            )
        if raw.get("scenario_generation_result") is not None:
            raw["scenario_generation_result"] = (
                CurrentScenarioGenerationResult.from_dict(
                    raw["scenario_generation_result"]
                )
            )
        raw["validation_result"] = ValidationResult.from_dict(
            raw["validation_result"]
        )
        raw["errors"] = tuple(raw.get("errors", ()))
        return cls(**raw)


def company_orchestration_bridge_result_content_hash(
    value: CompanyOrchestrationBridgeResult,
) -> str:
    if not isinstance(value, CompanyOrchestrationBridgeResult):
        raise TypeError(
            "value must be CompanyOrchestrationBridgeResult."
        )
    return _contract_hash(value)


def _failure(
    orchestration_input: MarketScenarioOrchestrationInput,
    orchestration_result: MarketScenarioOrchestrationResult,
    context: CompanyScenarioContext,
    code: str,
    message: str,
) -> CompanyOrchestrationBridgeResult:
    validation = ValidationResult(
        is_valid=False,
        score=0.0,
        errors=(
            ValidationIssue(
                code=code,
                severity="error",
                path="company_orchestration_bridge",
                message=message,
            ),
        ),
        failed_rules=(code,),
        validation_version=COMPANY_ORCHESTRATION_BRIDGE_SCHEMA_VERSION,
    )
    seed = CompanyOrchestrationBridgeResult(
        source_orchestration_input_hash=orchestration_input.content_hash,
        source_orchestration_result_hash=orchestration_result.content_hash,
        company_context_hash=context.content_hash,
        scenario_generation_result=None,
        validation_result=validation,
        errors=(message,),
    )
    return replace(seed, content_hash=_contract_hash(seed))


def run_company_orchestration_bridge(
    orchestration_input: MarketScenarioOrchestrationInput,
    orchestration_result: MarketScenarioOrchestrationResult,
    company_context: CompanyScenarioContext,
    generator: CurrentMarketScenarioGenerator,
) -> CompanyOrchestrationBridgeResult:
    """Rerun only scenario generation over frozen Phase 8 artifacts."""

    if not isinstance(
        orchestration_input,
        MarketScenarioOrchestrationInput,
    ):
        raise TypeError(
            "orchestration_input must be MarketScenarioOrchestrationInput."
        )
    if not isinstance(
        orchestration_result,
        MarketScenarioOrchestrationResult,
    ):
        raise TypeError(
            "orchestration_result must be MarketScenarioOrchestrationResult."
        )
    if not isinstance(company_context, CompanyScenarioContext):
        raise TypeError(
            "company_context must be CompanyScenarioContext."
        )
    if not isinstance(generator, CurrentMarketScenarioGenerator):
        raise TypeError(
            "generator must be CurrentMarketScenarioGenerator."
        )
    before = (
        orchestration_input.to_dict(),
        orchestration_result.to_dict(),
        company_context.to_dict(),
    )
    source_ok = (
        orchestration_input.content_hash
        == market_scenario_orchestration_input_content_hash(
            orchestration_input
        )
        and orchestration_result.content_hash
        == market_scenario_orchestration_result_content_hash(
            orchestration_result
        )
        and orchestration_result.orchestration_input_hash
        == orchestration_input.content_hash
    )
    if not source_ok:
        return _failure(
            orchestration_input,
            orchestration_result,
            company_context,
            "company_bridge_source_hash",
            "Phase 8 source orchestration hashes are invalid.",
        )
    state = orchestration_result.current_state
    retrieval = orchestration_result.pattern_retrieval_result
    if state is None or retrieval is None:
        return _failure(
            orchestration_input,
            orchestration_result,
            company_context,
            "company_bridge_missing_artifact",
            "Completed current-state and retrieval artifacts are required.",
        )
    context_validation = validate_company_scenario_context(
        company_context,
        premise=orchestration_input.frozen_technical_premise,
        state=state,
        exposures=orchestration_input.current_exposures,
    )
    if not context_validation.is_valid:
        return _failure(
            orchestration_input,
            orchestration_result,
            company_context,
            "company_bridge_context",
            "Company context does not match the frozen Phase 8 artifacts.",
        )
    scenario_result = generator.generate(
        orchestration_input.frozen_technical_premise,
        state,
        retrieval,
        orchestration_input.current_evidence,
        orchestration_input.current_exposures,
        orchestration_input.current_events,
        company_context=company_context,
    )
    validation = validate_current_scenario_generation_result(
        scenario_result,
        premise=orchestration_input.frozen_technical_premise,
        state=state,
        retrieval=retrieval,
    )
    errors = tuple(item.message for item in validation.errors)
    seed = CompanyOrchestrationBridgeResult(
        source_orchestration_input_hash=orchestration_input.content_hash,
        source_orchestration_result_hash=orchestration_result.content_hash,
        company_context_hash=company_context.content_hash,
        scenario_generation_result=scenario_result,
        validation_result=validation,
        errors=errors,
    )
    result = replace(seed, content_hash=_contract_hash(seed))
    after = (
        orchestration_input.to_dict(),
        orchestration_result.to_dict(),
        company_context.to_dict(),
    )
    if before != after:
        raise RuntimeError(
            "Company orchestration bridge mutated a frozen input."
        )
    return result


__all__ = [
    "COMPANY_ORCHESTRATION_BRIDGE_SCHEMA_VERSION",
    "CompanyOrchestrationBridgeResult",
    "company_orchestration_bridge_result_content_hash",
    "run_company_orchestration_bridge",
]
