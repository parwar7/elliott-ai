"""Output contract shared by providers and the CLI."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .timeframes import (
    EXAMPLE_CANONICAL_TIMEFRAMES,
    normalize_timeframe_name,
    timeframe_is_compatible_with_degree,
)

STANDARD_DEGREES = (
    "Grand Supercycle",
    "Supercycle",
    "Cycle",
    "Primary",
    "Intermediate",
    "Minor",
    "Minute",
    "Minuette",
    "Subminuette",
    "Unassigned",
)

VERIFICATION_REASON_CODES = (
    "parent_not_completed",
    "parent_degree_unassigned",
    "parent_pivot_missing",
    "parent_timeframe_invalid",
    "parent_invalidation_missing",
    "parent_terminal_with_children",
    "unsupported_parent_structure",
    "child_link_mismatch",
    "child_sequence_invalid",
    "child_not_completed",
    "child_degree_not_confirmed",
    "child_pivot_missing",
    "child_timeframe_invalid",
    "child_timeframe_inconsistent",
    "child_not_structurally_verified",
    "child_invalidation_missing",
    "hierarchy_degree_invalid",
    "child_outside_parent",
    "parent_child_boundary_mismatch",
    "child_pivot_chain_discontinuous",
    "hard_price_rule_failure",
)

HIERARCHY_METADATA_KINDS = (
    "origin_control",
    "gap_control",
)


# New model output uses this stable, human-readable anchor form. Historical
# stored runs are parsed compatibly by the agent and are never rewritten.
CANONICAL_ANALYSIS_ANCHOR_FORMAT = "YYYY-MM-DD @ price"
CANONICAL_ANALYSIS_ANCHOR_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}\s@\s[-+]?(?:\d+(?:\.\d+)?|\.\d+)$"
)
_CANONICAL_ANALYSIS_ANCHOR_RE = re.compile(CANONICAL_ANALYSIS_ANCHOR_PATTERN)


def is_canonical_analysis_anchor(value: Any) -> bool:
    """Return whether a newly generated analysis anchor uses the required form."""
    return isinstance(value, str) and bool(
        _CANONICAL_ANALYSIS_ANCHOR_RE.fullmatch(value.strip())
    )

DEGREE_TIMEFRAMES = {
    "Grand Supercycle": ("monthly", "weekly"),
    "Supercycle": ("monthly", "weekly"),
    "Cycle": ("monthly", "weekly"),
    "Primary": ("daily", "4h"),
    "Intermediate": ("daily", "4h"),
    "Minor": ("daily", "4h"),
    "Minute": ("1h", "15m"),
    "Minuette": ("1h", "15m"),
    "Subminuette": ("1h", "15m"),
    "Unassigned": ("unknown",),
}

CANONICAL_TIMEFRAME_PATTERN = (
    r"^(?:monthly|weekly|daily|[1-9]\d*[dhm]|unknown)$"
)

SEQUENCE_POSITIONS = (
    "1",
    "2",
    "3",
    "4",
    "5",
    "A",
    "B",
    "C",
    "D",
    "E",
    "W",
    "X",
    "Y",
    "X2",
    "Z",
    "0",
    "unknown",
)

INDICATOR_EVIDENCE_STATUSES = (
    "supportive",
    "contradictory",
    "neutral",
    "unavailable",
    "incomparable",
)

INDICATOR_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "observation",
        "supporting_evidence",
        "contradictory_evidence",
        "unavailable_or_incomparable_evidence",
        "evidence_needed_for_confirmation",
        "structural_invalidation",
        "policy",
    ],
    "properties": {
        "status": {"type": "string", "enum": list(INDICATOR_EVIDENCE_STATUSES)},
        "observation": {"type": "string"},
        "supporting_evidence": {"type": "array", "items": {"type": "string"}},
        "contradictory_evidence": {"type": "array", "items": {"type": "string"}},
        "unavailable_or_incomparable_evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "evidence_needed_for_confirmation": {
            "type": "array",
            "items": {"type": "string"},
        },
        "structural_invalidation": {"type": "boolean", "enum": [False]},
        "policy": {"type": "string"},
    },
}

RSI_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["availability", "warm_up_status", "comparability", "source", "evidence"],
    "properties": {
        "availability": {"type": "string", "enum": ["available", "unavailable"]},
        "value_at_wave_start": {"type": ["number", "null"]},
        "value_at_wave_end": {"type": ["number", "null"]},
        "minimum": {"type": ["number", "null"]},
        "maximum": {"type": ["number", "null"]},
        "slope_per_bar": {"type": ["number", "null"]},
        "proportion_above_50": {"type": ["number", "null"]},
        "proportion_below_50": {"type": ["number", "null"]},
        "comparison_with_prior_wave": {"type": ["object", "null"]},
        "divergence_candidate": {
            "type": ["string", "null"],
            "enum": ["bullish", "bearish", "none", None],
        },
        "divergence_strength_rsi_points": {"type": ["number", "null"]},
        "divergence_distance": {"type": ["object", "null"]},
        "warm_up_status": {"type": "string"},
        "comparability": {
            "type": "string",
            "enum": ["comparable", "incomparable", "unavailable"],
        },
        "source": {
            "type": "object",
            "required": [
                "provider",
                "venue",
                "symbol",
                "timeframe",
                "price_field",
                "period",
                "calculation_method",
                "feed_identity",
            ],
        },
        "evidence": INDICATOR_EVIDENCE_SCHEMA,
    },
}


ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "analysis_status",
        "symbol",
        "data_cutoff",
        "scope",
        "preferred_count",
        "alternate_counts",
        "confirmations",
        "invalidation_levels",
        "missing_data",
        "confidence",
        "risk_note",
        "citations",
    ],
    "properties": {
        "analysis_status": {
            "type": "string",
            "enum": [
                "complete",
                "provisional",
                "insufficient_data",
                "evidence_packet_only",
            ],
        },
        "symbol": {"type": "string"},
        "data_cutoff": {"type": ["string", "null"]},
        "scope": {"type": "string"},
        "preferred_count": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "degree",
                    "wave",
                    "start",
                    "end",
                    "structure",
                    "status",
                    "evidence",
                ],
                "properties": {
                    "degree": {"type": "string"},
                    "wave": {"type": "string"},
                    "start": {
                        "type": ["string", "null"],
                        "pattern": CANONICAL_ANALYSIS_ANCHOR_PATTERN,
                        "description": (
                            "When present, use "
                            f"{CANONICAL_ANALYSIS_ANCHOR_FORMAT}."
                        ),
                    },
                    "end": {
                        "type": ["string", "null"],
                        "pattern": CANONICAL_ANALYSIS_ANCHOR_PATTERN,
                        "description": (
                            "When present, use "
                            f"{CANONICAL_ANALYSIS_ANCHOR_FORMAT}."
                        ),
                    },
                    "structure": {"type": "string"},
                    "status": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "alternate_counts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "trigger", "invalidation"],
                "properties": {
                    "description": {"type": "string"},
                    "trigger": {"type": "string"},
                    "invalidation": {"type": "string"},
                    "hypothesis_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["unresolved", "structurally_valid"],
                    },
                    "supporting_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "contradictory_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "unavailable_or_incomparable_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence_needed_for_confirmation": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "confirmations": {"type": "array", "items": {"type": "string"}},
        "invalidation_levels": {
            "type": "array",
            "items": {"type": "string"},
        },
        "missing_data": {"type": "array", "items": {"type": "string"}},
        "confidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["structure", "degree", "active_wave"],
            "properties": {
                "structure": {"type": "number", "minimum": 0, "maximum": 1},
                "degree": {"type": "number", "minimum": 0, "maximum": 1},
                "active_wave": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "risk_note": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence_id", "claim"],
                "properties": {
                    "evidence_id": {"type": "string"},
                    "claim": {"type": "string"},
                },
            },
        },
    },
}


_ANCHOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["date", "price"],
    "properties": {
        "date": {"type": ["string", "null"]},
        "price": {"type": ["number", "null"]},
    },
}

_HIERARCHY_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "metadata_id",
        "kind",
        "description",
        "start",
        "end",
        "evidence",
        "concerns",
    ],
    "properties": {
        "metadata_id": {"type": "string"},
        "kind": {"type": "string", "enum": list(HIERARCHY_METADATA_KINDS)},
        "description": {"type": "string"},
        "start": _ANCHOR_SCHEMA,
        "end": _ANCHOR_SCHEMA,
        "evidence": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
}


DEGREE_RESOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "resolution_status",
        "symbol",
        "source_run_id",
        "data_cutoff",
        "scale_mode",
        "scope",
        "degree_hierarchy",
        "active_position",
        "alternate_counts",
        "invalidation_levels",
        "unresolved_items",
        "confidence",
        "citations",
        "risk_note",
    ],
    "properties": {
        "resolution_status": {
            "type": "string",
            "enum": ["final", "provisional", "insufficient_data", "evidence_packet_only"],
        },
        "symbol": {"type": "string"},
        "source_run_id": {"type": "integer"},
        "data_cutoff": {"type": ["string", "null"]},
        "scale_mode": {
            "type": "string",
            "enum": ["logarithmic", "arithmetic", "mixed", "undetermined"],
        },
        "scope": {"type": "string"},
        "degree_hierarchy": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "wave_id",
                    "parent_wave_id",
                    "degree",
                    "degree_status",
                    "degree_confidence",
                    "wave",
                    "sequence_position",
                    "timeframe",
                    "direction",
                    "start",
                    "end",
                    "structure",
                    "completion_status",
                    "child_structure_status",
                    "terminal_at_available_resolution",
                    "child_wave_ids",
                    "evidence",
                    "confirmations",
                    "concerns",
                    "invalidation",
                ],
                "properties": {
                    "wave_id": {"type": "string"},
                    "parent_wave_id": {"type": ["string", "null"]},
                    "degree": {"type": "string", "enum": list(STANDARD_DEGREES)},
                    "degree_status": {
                        "type": "string",
                        "enum": ["confirmed", "candidate", "unassigned"],
                    },
                    "degree_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "wave": {"type": "string"},
                    "sequence_position": {
                        "type": "string",
                        "enum": list(SEQUENCE_POSITIONS),
                    },
                    "timeframe": {
                        "type": "string",
                        "pattern": CANONICAL_TIMEFRAME_PATTERN,
                        "description": (
                            "Canonical evidence timeframe. Adaptive workflows may use any "
                            "provider-advertised interval; common values include "
                            + ", ".join(EXAMPLE_CANONICAL_TIMEFRAMES)
                            + "."
                        ),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "sideways", "unknown"],
                    },
                    "start": _ANCHOR_SCHEMA,
                    "end": _ANCHOR_SCHEMA,
                    "structure": {"type": "string"},
                    "completion_status": {
                        "type": "string",
                        "enum": ["completed", "active", "projected", "unknown"],
                    },
                    "child_structure_status": {
                        "type": "string",
                        "enum": ["verified", "unproven", "not_required"],
                    },
                    "verification_reason_codes": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(VERIFICATION_REASON_CODES),
                        },
                    },
                    "terminal_at_available_resolution": {"type": "boolean"},
                    "child_wave_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confirmations": {"type": "array", "items": {"type": "string"}},
                    "concerns": {"type": "array", "items": {"type": "string"}},
                    "invalidation": {"type": ["string", "null"]},
                    "rsi_comparison_wave_id": {"type": ["string", "null"]},
                    "fingerprint_comparison_wave_id": {"type": ["string", "null"]},
                    "fingerprint_comparison_relationship": {
                        "type": ["string", "null"],
                        "enum": ["retracement", "extension", "comparison", None],
                    },
                },
            },
        },
        "hierarchy_metadata": {
            "type": "array",
            "items": _HIERARCHY_METADATA_SCHEMA,
        },
        "active_position": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "wave_ids", "confirmation", "invalidation"],
            "properties": {
                "summary": {"type": "string"},
                "wave_ids": {"type": "array", "items": {"type": "string"}},
                "confirmation": {"type": "string"},
                "invalidation": {"type": "string"},
            },
        },
        "alternate_counts": ANALYSIS_SCHEMA["properties"]["alternate_counts"],
        "invalidation_levels": ANALYSIS_SCHEMA["properties"]["invalidation_levels"],
        "unresolved_items": {"type": "array", "items": {"type": "string"}},
        "confidence": ANALYSIS_SCHEMA["properties"]["confidence"],
        "citations": ANALYSIS_SCHEMA["properties"]["citations"],
        "risk_note": {"type": "string"},
    },
}


REQUIRED_ANALYSIS_FIELDS = tuple(ANALYSIS_SCHEMA["required"])


def _validate_alternate_counts(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        return []
    errors: list[str] = []
    evidence_fields = (
        "supporting_evidence",
        "contradictory_evidence",
        "unavailable_or_incomparable_evidence",
        "evidence_needed_for_confirmation",
    )
    for index, item in enumerate(value, start=1):
        prefix = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        status = item.get("status")
        if status is not None and status not in {"unresolved", "structurally_valid"}:
            errors.append(f"{prefix}.status is invalid.")
        hypothesis_id = item.get("hypothesis_id")
        if hypothesis_id is not None and not isinstance(hypothesis_id, str):
            errors.append(f"{prefix}.hypothesis_id must be a string.")
        for field in evidence_fields:
            evidence = item.get(field)
            if evidence is not None and (
                not isinstance(evidence, list)
                or not all(isinstance(entry, str) for entry in evidence)
            ):
                errors.append(f"{prefix}.{field} must be an array of strings.")
    return errors


def validate_analysis_shape(value: Any) -> list[str]:
    """Return lightweight validation errors without an external schema package."""
    if not isinstance(value, dict):
        return ["Provider output must be a JSON object."]
    errors = [
        f"Missing required field: {field}"
        for field in REQUIRED_ANALYSIS_FIELDS
        if field not in value
    ]
    confidence = value.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, dict):
            errors.append("confidence must be an object.")
        else:
            for field in ("structure", "degree", "active_wave"):
                score = confidence.get(field)
                valid = (
                    isinstance(score, (int, float))
                    and not isinstance(score, bool)
                    and 0 <= score <= 1
                )
                if not valid:
                    errors.append(f"confidence.{field} must be between 0 and 1.")
    preferred_count = value.get("preferred_count")
    if isinstance(preferred_count, list):
        for index, item in enumerate(preferred_count, start=1):
            if not isinstance(item, dict):
                continue
            for field in ("start", "end"):
                anchor = item.get(field)
                if anchor is not None and not is_canonical_analysis_anchor(anchor):
                    errors.append(
                        f"preferred_count[{index}].{field} must use "
                        f"{CANONICAL_ANALYSIS_ANCHOR_FORMAT} when present."
                    )
    errors.extend(_validate_alternate_counts(value.get("alternate_counts"), "alternate_counts"))
    return errors


def _valid_score(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 1
    )


def _validate_anchor(anchor: Any, path: str, *, start: bool) -> list[str]:
    if not isinstance(anchor, dict):
        return [f"{path} must be an object."]
    errors: list[str] = []
    date = anchor.get("date")
    price = anchor.get("price")
    if start and not isinstance(date, str):
        errors.append(f"{path}.date must be a string for a wave start.")
    elif date is not None and not isinstance(date, str):
        errors.append(f"{path}.date must be a string or null.")
    elif isinstance(date, str):
        try:
            datetime.fromisoformat(date.strip().replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{path}.date must be an ISO-8601 date or datetime.")
    if start and (
        not isinstance(price, (int, float)) or isinstance(price, bool)
    ):
        errors.append(f"{path}.price must be numeric for a wave start.")
    elif price is not None and (
        not isinstance(price, (int, float)) or isinstance(price, bool)
    ):
        errors.append(f"{path}.price must be numeric or null.")
    return errors


def _validate_hierarchy_metadata(value: Any) -> tuple[list[str], set[str]]:
    """Validate non-wave control intervals kept outside degree_hierarchy."""
    if value is None:
        return [], set()
    if not isinstance(value, list):
        return ["hierarchy_metadata must be an array."], set()

    errors: list[str] = []
    identifiers: set[str] = set()
    required = _HIERARCHY_METADATA_SCHEMA["required"]
    for index, item in enumerate(value, start=1):
        prefix = f"hierarchy_metadata[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        unexpected = set(item) - set(_HIERARCHY_METADATA_SCHEMA["properties"])
        if unexpected:
            errors.append(
                f"{prefix} has unsupported fields: " + ", ".join(sorted(unexpected)) + "."
            )
        for field in required:
            if field not in item:
                errors.append(f"{prefix} is missing {field}.")
        metadata_id = item.get("metadata_id")
        if not isinstance(metadata_id, str) or not metadata_id.strip():
            errors.append(f"{prefix}.metadata_id must be a non-empty string.")
        elif metadata_id in identifiers:
            errors.append(f"Duplicate hierarchy metadata ID: {metadata_id!r}.")
        else:
            identifiers.add(metadata_id)
        if item.get("kind") not in HIERARCHY_METADATA_KINDS:
            errors.append(f"{prefix}.kind is invalid.")
        if not isinstance(item.get("description"), str) or not item.get("description", "").strip():
            errors.append(f"{prefix}.description must be a non-empty string.")
        for side in ("start", "end"):
            errors.extend(_validate_anchor(item.get(side), f"{prefix}.{side}", start=False))
            anchor = item.get(side)
            if not isinstance(anchor, dict) or not isinstance(anchor.get("date"), str):
                errors.append(f"{prefix}.{side}.date must be a string.")
        for field in ("evidence", "concerns"):
            entries = item.get(field)
            if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
                errors.append(f"{prefix}.{field} must be an array of strings.")
    return errors, identifiers


def validate_degree_resolution_shape(
    value: Any, *, source_run_id: int | None = None
) -> list[str]:
    """Validate the strict flat parent-child degree contract."""
    if not isinstance(value, dict):
        return ["Degree resolution output must be a JSON object."]

    errors = [
        f"Missing required field: {field}"
        for field in DEGREE_RESOLUTION_SCHEMA["required"]
        if field not in value
    ]
    status = value.get("resolution_status")
    allowed_statuses = {"final", "provisional", "insufficient_data", "evidence_packet_only"}
    if status not in allowed_statuses:
        errors.append("resolution_status is invalid.")
    if not isinstance(value.get("symbol"), str) or not value.get("symbol", "").strip():
        errors.append("symbol must be a non-empty string.")
    if not isinstance(value.get("source_run_id"), int) or isinstance(
        value.get("source_run_id"), bool
    ):
        errors.append("source_run_id must be an integer.")
    if source_run_id is not None and value.get("source_run_id") != source_run_id:
        errors.append(
            f"source_run_id must equal the requested run {source_run_id}."
        )
    if value.get("scale_mode") not in {
        "logarithmic",
        "arithmetic",
        "mixed",
        "undetermined",
    }:
        errors.append("scale_mode is invalid.")
    if not isinstance(value.get("scope"), str):
        errors.append("scope must be a string.")
    for field in ("alternate_counts", "invalidation_levels", "unresolved_items", "citations"):
        if not isinstance(value.get(field), list):
            errors.append(f"{field} must be an array.")
    errors.extend(_validate_alternate_counts(value.get("alternate_counts"), "alternate_counts"))
    metadata_errors, metadata_ids = _validate_hierarchy_metadata(
        value.get("hierarchy_metadata")
    )
    errors.extend(metadata_errors)
    active = value.get("active_position")
    if not isinstance(active, dict):
        errors.append("active_position must be an object.")
    else:
        for field in ("summary", "confirmation", "invalidation"):
            if not isinstance(active.get(field), str):
                errors.append(f"active_position.{field} must be a string.")
        if not isinstance(active.get("wave_ids"), list) or not all(
            isinstance(item, str) for item in active.get("wave_ids", [])
        ):
            errors.append("active_position.wave_ids must be an array of strings.")

    confidence = value.get("confidence")
    if not isinstance(confidence, dict):
        errors.append("confidence must be an object.")
    else:
        for field in ("structure", "degree", "active_wave"):
            if not _valid_score(confidence.get(field)):
                errors.append(f"confidence.{field} must be between 0 and 1.")

    nodes = value.get("degree_hierarchy")
    if not isinstance(nodes, list):
        errors.append("degree_hierarchy must be an array.")
        return errors
    if status in {"final", "provisional"} and not nodes:
        errors.append(f"A {status} resolution requires at least one wave node.")

    node_by_id: dict[str, dict[str, Any]] = {}
    required_node_fields = DEGREE_RESOLUTION_SCHEMA["properties"]["degree_hierarchy"][
        "items"
    ]["required"]
    for index, node in enumerate(nodes, start=1):
        prefix = f"degree_hierarchy[{index}]"
        if not isinstance(node, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        for field in required_node_fields:
            if field not in node:
                errors.append(f"{prefix} is missing {field}.")
        wave_id = node.get("wave_id")
        if not isinstance(wave_id, str) or not wave_id.strip():
            errors.append(f"{prefix}.wave_id must be a non-empty string.")
        elif wave_id in node_by_id:
            errors.append(f"Duplicate wave_id: {wave_id!r}.")
        else:
            node_by_id[wave_id] = node
        degree = node.get("degree")
        if degree not in STANDARD_DEGREES:
            errors.append(f"{prefix}.degree must use a standard Elliott degree.")
        if node.get("degree_status") not in {"confirmed", "candidate", "unassigned"}:
            errors.append(f"{prefix}.degree_status is invalid.")
        if not _valid_score(node.get("degree_confidence")):
            errors.append(f"{prefix}.degree_confidence must be between 0 and 1.")
        if node.get("sequence_position") not in SEQUENCE_POSITIONS:
            errors.append(f"{prefix}.sequence_position is invalid.")
        if not isinstance(node.get("wave"), str) or not node.get("wave", "").strip():
            errors.append(f"{prefix}.wave must be a non-empty string.")
        if not isinstance(node.get("structure"), str) or not node.get("structure", "").strip():
            errors.append(f"{prefix}.structure must be a non-empty string.")
        parent_id = node.get("parent_wave_id")
        if parent_id is not None and not isinstance(parent_id, str):
            errors.append(f"{prefix}.parent_wave_id must be a string or null.")
        timeframe = node.get("timeframe")
        canonical_timeframe = None
        if timeframe == "unknown":
            canonical_timeframe = "unknown"
        else:
            try:
                canonical_timeframe = normalize_timeframe_name(timeframe)
            except ValueError:
                canonical_timeframe = None
        if canonical_timeframe is None or canonical_timeframe != timeframe:
            errors.append(f"{prefix}.timeframe is invalid.")
        elif not timeframe_is_compatible_with_degree(degree, timeframe):
            errors.append(
                f"{prefix}.timeframe {timeframe!r} is outside the mandatory scale for "
                f"{degree} under the permitted granularity policy."
            )
        if node.get("direction") not in {"up", "down", "sideways", "unknown"}:
            errors.append(f"{prefix}.direction is invalid.")
        if node.get("completion_status") not in {"completed", "active", "projected", "unknown"}:
            errors.append(f"{prefix}.completion_status is invalid.")
        child_status = node.get("child_structure_status")
        if child_status not in {"verified", "unproven", "not_required"}:
            errors.append(f"{prefix}.child_structure_status is invalid.")
        reason_codes = node.get("verification_reason_codes")
        if reason_codes is not None and (
            not isinstance(reason_codes, list)
            or not all(code in VERIFICATION_REASON_CODES for code in reason_codes)
        ):
            errors.append(f"{prefix}.verification_reason_codes is invalid.")
        terminal = node.get("terminal_at_available_resolution")
        if not isinstance(terminal, bool):
            errors.append(f"{prefix}.terminal_at_available_resolution must be boolean.")
        if child_status == "not_required" and terminal is not True:
            errors.append(
                f"{prefix} may use child_structure_status not_required only at the available resolution limit."
            )
        child_ids = node.get("child_wave_ids")
        if not isinstance(child_ids, list) or not all(isinstance(item, str) for item in child_ids):
            errors.append(f"{prefix}.child_wave_ids must be an array of strings.")
        elif child_status == "verified" and not child_ids:
            errors.append(f"{prefix} marks children verified but supplies no child_wave_ids.")
        for field in ("evidence", "confirmations", "concerns"):
            items = node.get(field)
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                errors.append(f"{prefix}.{field} must be an array of strings.")
        comparison_wave_id = node.get("rsi_comparison_wave_id")
        if comparison_wave_id is not None and not isinstance(comparison_wave_id, str):
            errors.append(f"{prefix}.rsi_comparison_wave_id must be a string or null.")
        fingerprint_comparison_id = node.get("fingerprint_comparison_wave_id")
        if fingerprint_comparison_id is not None and not isinstance(
            fingerprint_comparison_id, str
        ):
            errors.append(
                f"{prefix}.fingerprint_comparison_wave_id must be a string or null."
            )
        relationship = node.get("fingerprint_comparison_relationship")
        if relationship not in (None, "retracement", "extension", "comparison"):
            errors.append(
                f"{prefix}.fingerprint_comparison_relationship is invalid."
            )
        errors.extend(_validate_anchor(node.get("start"), f"{prefix}.start", start=True))
        errors.extend(_validate_anchor(node.get("end"), f"{prefix}.end", start=False))
        if node.get("completion_status") == "completed" and isinstance(node.get("end"), dict):
            if node["end"].get("date") is None or node["end"].get("price") is None:
                errors.append(f"{prefix}.end must be complete for a completed wave.")

    degree_rank = {degree: index for index, degree in enumerate(STANDARD_DEGREES[:-1])}
    actual_children: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("wave_id"), str):
            continue
        wave_id = node["wave_id"]
        parent_id = node.get("parent_wave_id")
        if parent_id is not None:
            if parent_id not in node_by_id:
                errors.append(f"Wave {wave_id!r} references missing parent {parent_id!r}.")
            elif parent_id == wave_id:
                errors.append(f"Wave {wave_id!r} cannot be its own parent.")
            else:
                actual_children.setdefault(parent_id, set()).add(wave_id)
                parent_degree = node_by_id[parent_id].get("degree")
                child_degree = node.get("degree")
                if parent_degree in degree_rank and child_degree in degree_rank:
                    if degree_rank[child_degree] <= degree_rank[parent_degree]:
                        errors.append(
                            f"Wave {wave_id!r} degree must be lower than parent {parent_id!r}."
                        )
        for child_id in node.get("child_wave_ids", []):
            if child_id not in node_by_id:
                errors.append(f"Wave {wave_id!r} references missing child {child_id!r}.")
        comparison_wave_id = node.get("rsi_comparison_wave_id")
        if isinstance(comparison_wave_id, str):
            if comparison_wave_id == wave_id:
                errors.append(f"Wave {wave_id!r} cannot compare RSI with itself.")
            elif comparison_wave_id not in node_by_id:
                errors.append(
                    f"Wave {wave_id!r} references missing RSI comparison wave "
                    f"{comparison_wave_id!r}."
                )
        fingerprint_comparison_id = node.get("fingerprint_comparison_wave_id")
        if isinstance(fingerprint_comparison_id, str):
            if fingerprint_comparison_id == wave_id:
                errors.append(f"Wave {wave_id!r} cannot compare its fingerprint with itself.")
            elif fingerprint_comparison_id not in node_by_id:
                errors.append(
                    f"Wave {wave_id!r} references missing fingerprint comparison wave "
                    f"{fingerprint_comparison_id!r}."
                )

    for wave_id, node in node_by_id.items():
        if wave_id in metadata_ids:
            errors.append(
                f"Wave {wave_id!r} cannot also be hierarchy metadata."
            )
        declared = set(node.get("child_wave_ids", []))
        actual = actual_children.get(wave_id, set())
        if declared != actual:
            errors.append(
                f"Wave {wave_id!r} child_wave_ids do not match child parent_wave_id links."
            )
    active_ids = value.get("active_position", {}).get("wave_ids", []) if isinstance(
        value.get("active_position"), dict
    ) else []
    for wave_id in active_ids:
        if wave_id not in node_by_id:
            errors.append(f"active_position references missing wave {wave_id!r}.")
    return errors
