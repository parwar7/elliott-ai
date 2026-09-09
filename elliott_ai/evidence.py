"""Versioned, non-probabilistic evidence items shared across analytical layers."""

from __future__ import annotations

import json
from typing import Any, Mapping


EVIDENCE_CONTRACT_VERSION = "1.0.0"
EVIDENCE_CALCULATION_VERSION = "evidence-calc-1.0.0"
EVIDENCE_STATUSES = frozenset(
    {
        "supportive",
        "contradictory",
        "neutral",
        "unavailable",
        "incomparable",
        "invalidating",
    }
)
EVIDENCE_TYPES = frozenset(
    {
        "rsi",
        "volume",
        "ewo",
        "macd",
        "atr",
        "volatility",
        "fibonacci",
        "channel",
        "indicator",
        "structural",
        "structural_rule",
        "explicit_price_invalidation",
    }
)
INDICATOR_EVIDENCE_TYPES = frozenset(
    {"rsi", "volume", "ewo", "macd", "atr", "volatility", "indicator"}
)
INVALIDATING_EVIDENCE_TYPES = frozenset(
    {"structural", "structural_rule", "explicit_price_invalidation"}
)


def make_evidence_item(
    *,
    evidence_type: str,
    feature_name: str,
    observed_value: Any,
    status: str,
    reason: str,
    comparison_or_expected_condition: Any = None,
    source_wave_or_pivot: Any = None,
    comparison_wave_or_pivot: Any = None,
    hard_rule: bool = False,
    data_quality_status: str = "available",
    provenance: Mapping[str, Any] | None = None,
    calculation_version: str = EVIDENCE_CALCULATION_VERSION,
) -> dict[str, Any]:
    """Build one serializable evidence item and enforce invalidation policy."""
    normalized_type = str(evidence_type).strip().lower()
    normalized_status = str(status).strip().lower()
    if normalized_status not in EVIDENCE_STATUSES:
        raise ValueError(f"Unsupported evidence status: {status!r}")
    if not normalized_type:
        raise ValueError("evidence_type must be non-empty.")
    if normalized_type not in EVIDENCE_TYPES:
        raise ValueError(f"Unsupported evidence type: {evidence_type!r}")
    if not str(feature_name).strip():
        raise ValueError("feature_name must be non-empty.")
    if not str(reason).strip():
        raise ValueError("reason must be non-empty.")
    if normalized_status == "invalidating":
        if not hard_rule or normalized_type not in INVALIDATING_EVIDENCE_TYPES:
            raise ValueError(
                "Only hard structural rules and explicit price invalidations may "
                "use invalidating evidence."
            )
    if normalized_type in INDICATOR_EVIDENCE_TYPES and (
        normalized_status == "invalidating" or hard_rule
    ):
        raise ValueError("Indicator evidence cannot be a hard-rule invalidation.")
    if hard_rule and normalized_type not in INVALIDATING_EVIDENCE_TYPES:
        raise ValueError("hard_rule is reserved for structure and explicit price levels.")
    return {
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": normalized_type,
        "feature_name": str(feature_name),
        "observed_value": observed_value,
        "comparison_or_expected_condition": comparison_or_expected_condition,
        "status": normalized_status,
        "reason": str(reason),
        "source_wave_or_pivot": source_wave_or_pivot,
        "comparison_wave_or_pivot": comparison_wave_or_pivot,
        "hard_rule": bool(hard_rule),
        "data_quality_status": str(data_quality_status),
        "provenance": dict(provenance or {}),
        "calculation_version": str(calculation_version),
    }


def validate_evidence_item(item: Mapping[str, Any]) -> list[str]:
    """Return contract errors without changing the supplied evidence item."""
    required = (
        "evidence_contract_version",
        "evidence_type",
        "feature_name",
        "observed_value",
        "comparison_or_expected_condition",
        "status",
        "reason",
        "source_wave_or_pivot",
        "comparison_wave_or_pivot",
        "hard_rule",
        "data_quality_status",
        "provenance",
        "calculation_version",
    )
    errors = [f"Missing evidence field: {field}." for field in required if field not in item]
    try:
        if not errors:
            make_evidence_item(
                evidence_type=str(item["evidence_type"]),
                feature_name=str(item["feature_name"]),
                observed_value=item["observed_value"],
                comparison_or_expected_condition=item[
                    "comparison_or_expected_condition"
                ],
                status=str(item["status"]),
                reason=str(item["reason"]),
                source_wave_or_pivot=item["source_wave_or_pivot"],
                comparison_wave_or_pivot=item["comparison_wave_or_pivot"],
                hard_rule=bool(item["hard_rule"]),
                data_quality_status=str(item["data_quality_status"]),
                provenance=(
                    item["provenance"]
                    if isinstance(item["provenance"], Mapping)
                    else {}
                ),
                calculation_version=str(item["calculation_version"]),
            )
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def serialize_evidence_item(item: Mapping[str, Any]) -> str:
    errors = validate_evidence_item(item)
    if errors:
        raise ValueError("Invalid evidence item: " + " ".join(errors))
    return json.dumps(dict(item), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
