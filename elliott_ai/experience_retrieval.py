"""Deterministic presentation ordering for eligible experience analogues.

Phase 5A.4 consumes only Phase 5A.3 comparison records. It partitions those
records into explicit quality tiers and orders them with a versioned
lexicographic policy. It does not read reviewed outcomes, predict future
market behavior, resolve Elliott counts, or make trading decisions.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .experience_analogue import (
    ANALOGUE_COMPARISON_CALCULATION_VERSION,
    ANALOGUE_COMPARISON_SCHEMA_VERSION,
    COMPARISON_SPECIFICATION_VERSION,
    DIMENSION_DEFINITIONS,
    INCOMPARABLE_STATES,
    MISSING_STATES,
    OMITTED_STATES,
    analogue_comparison_content_hash,
    comparison_specification,
)
from .experience_comparison import (
    COMPARISON_CALCULATION_VERSION,
    COMPARISON_SCHEMA_VERSION,
    COMPATIBILITY_MATRIX_VERSION,
    FAMILY_MATRIX_VERSION,
    ROLE_CLASS_VERSION,
)


ANALOGUE_RETRIEVAL_SCHEMA_VERSION = "experience-analogue-retrieval-1.0.0"
ANALOGUE_RETRIEVAL_CALCULATION_VERSION = (
    "experience-analogue-retrieval-calc-1.0.0"
)
RETRIEVAL_POLICY_SCHEMA_VERSION = "experience-retrieval-policy-1.0.0"
DEFAULT_RETRIEVAL_POLICY_ID = "endpoint-evidence-lexicographic"
DEFAULT_RETRIEVAL_POLICY_VERSION = "1.0.0"
DEFAULT_RESULT_LIMIT = 10
MAXIMUM_RESULT_LIMIT = 100

GROUP_PRECEDENCE = (
    "endpoint_identity_role",
    "structural_geometry",
    "confirmation_at_cutoff",
    "momentum_context",
    "volume_context",
    "volatility_context",
    "multitimeframe_context",
    "market_context",
)

MANDATORY_DIMENSIONS = (
    "endpoint_position",
    "structural_role_class",
    "parent_family",
    "direction",
    "elliott_degree",
    "internal_structure_family",
    "child_structure_status",
    "completion_status_at_cutoff",
    "start_pivot_alignment",
    "end_pivot_alignment",
)

SUMMARY_KEY_PRECEDENCE = (
    ("incomparable_count", "ascending"),
    ("missing_count", "ascending"),
    ("omitted_count", "ascending"),
    ("comparable_count", "descending"),
    ("outside_tolerance_count", "ascending"),
    ("within_tolerance_count", "descending"),
    ("categorical_mismatch_count", "ascending"),
    ("exact_categorical_match_count", "descending"),
)

TIER_LABELS = {
    1: "Tier 1: exact structural analogue",
    2: "Tier 2: compatible validated analogue",
    3: "Tier 3: broader or partially available analogue",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_payload(value: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(value))
    payload.pop("content_hash", None)
    return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


def analogue_retrieval_content_hash(value: Mapping[str, Any]) -> str:
    """Return the canonical Phase 5A.4 content hash."""
    return _hash_payload(value)


def _policy_payload() -> dict[str, Any]:
    dimension_precedence = [item.dimension_id for item in DIMENSION_DEFINITIONS]
    eligible_dimensions = [
        {
            "dimension_id": item.dimension_id,
            "group": item.group,
            "allowed_comparison_levels": list(item.allowed_levels),
            "precedence": index,
        }
        for index, item in enumerate(DIMENSION_DEFINITIONS, start=1)
    ]
    group_component_rules = [
        {
            "key": key,
            "direction": direction,
            "missing_or_incomparable_never_improves_order": True,
        }
        for key, direction in SUMMARY_KEY_PRECEDENCE
    ]
    return {
        "schema_version": RETRIEVAL_POLICY_SCHEMA_VERSION,
        "policy_id": DEFAULT_RETRIEVAL_POLICY_ID,
        "policy_version": DEFAULT_RETRIEVAL_POLICY_VERSION,
        "retrieval_schema_version": ANALOGUE_RETRIEVAL_SCHEMA_VERSION,
        "retrieval_calculation_version": ANALOGUE_RETRIEVAL_CALCULATION_VERSION,
        "comparison_specification_version": COMPARISON_SPECIFICATION_VERSION,
        "phase5a2_filter_versions": {
            "schema_version": COMPARISON_SCHEMA_VERSION,
            "calculation_version": COMPARISON_CALCULATION_VERSION,
            "role_class_version": ROLE_CLASS_VERSION,
            "compatibility_matrix_version": COMPATIBILITY_MATRIX_VERSION,
            "family_matrix_version": FAMILY_MATRIX_VERSION,
        },
        "allowed_phase5a2_comparison_levels": [1, 2, 3, 4],
        "eligible_phase5a3_dimensions": eligible_dimensions,
        "dimension_precedence": dimension_precedence,
        "mandatory_dimensions": list(MANDATORY_DIMENSIONS),
        "grouping_precedence": list(GROUP_PRECEDENCE),
        "retrieval_tiers": [
            {
                "tier": 1,
                "label": TIER_LABELS[1],
                "requirements": [
                    "Phase 5A.2 comparison Level 1.",
                    "Exact structural-role and family compatibility.",
                    "Every mandatory structural dimension is comparable.",
                    "No feed or provenance incomparable dimensions.",
                ],
            },
            {
                "tier": 2,
                "label": TIER_LABELS[2],
                "requirements": [
                    "Phase 5A.2 comparison Level 1, 2, or 3.",
                    "Exact or compatible structural-role relationship.",
                    "Every mandatory structural dimension is comparable.",
                    "The candidate did not satisfy every Tier 1 condition.",
                ],
            },
            {
                "tier": 3,
                "label": TIER_LABELS[3],
                "requirements": [
                    "The candidate remains Phase 5A.2 eligible.",
                    "It is context-only, cross-market, or has unavailable mandatory evidence.",
                    "The broader or partial status must remain visible.",
                ],
            },
        ],
        "ordering_model": {
            "kind": "lexicographic",
            "synthetic_overall_score": False,
            "primary_keys": [
                {"key": "retrieval_tier", "direction": "ascending"},
                {"key": "comparison_level", "direction": "ascending"},
                {"key": "mandatory_incomparable_count", "direction": "ascending"},
                {"key": "mandatory_missing_count", "direction": "ascending"},
                {"key": "mandatory_omitted_count", "direction": "ascending"},
                {"key": "mandatory_comparable_count", "direction": "descending"},
            ],
            "group_component_precedence": group_component_rules,
            "aggregate_component_precedence": group_component_rules,
            "final_tie_breaker": {
                "key": "experience_case_id",
                "direction": "ascending",
            },
            "grouped_numeric_distance": False,
            "cutoff_proximity_used": False,
        },
        "missing_data_rules": {
            "states": sorted(MISSING_STATES),
            "policy": (
                "Missing values remain explicit, increase missing counts, and never become "
                "zero, a match, or a favorable distance."
            ),
        },
        "incomparability_rules": {
            "states": sorted(INCOMPARABLE_STATES),
            "policy": (
                "Feed and provenance incomparability remain explicit and increase "
                "incomparability counts; no cross-feed substitution is permitted."
            ),
        },
        "diversity_rules": {
            "enabled": False,
            "reason": (
                "No deterministic diversity matrix was approved for Phase 5A.4; "
                "diversity is deferred."
            ),
        },
        "maximum_results": {
            "default": DEFAULT_RESULT_LIMIT,
            "hard_maximum": MAXIMUM_RESULT_LIMIT,
        },
        "explanation_requirements": [
            "State why each case entered its tier.",
            "Expose every deterministic ordering key.",
            "Identify the first key that orders adjacent cases.",
            "Expose missing, incomparable, omitted, and warning states.",
            "Return the raw Phase 5A.3 dimension evidence.",
        ],
        "provenance_requirements": [
            "Accepted experience-case identity.",
            "Phase 5A.2 filter decision and hash.",
            "Phase 5A.3 comparison ID and hash.",
            "Immutable endpoint DNA, Phase 3 fingerprint, and Phase 4 snapshot hashes.",
            "Valid decision-time cutoffs.",
        ],
        "hash_generation_rules": {
            "algorithm": "sha256",
            "serialization": "ASCII canonical JSON with sorted keys and compact separators",
            "finite_numbers_only": True,
            "self_field_excluded": "content_hash",
            "timestamps_added": False,
        },
        "reviewed_outcome_policy": (
            "Reviewed outcomes, confirmation DNA, and post-cutoff evidence are forbidden "
            "filtering, comparison, tier, ordering, selection, tie-break, and hash inputs."
        ),
        "interpretation_boundary": (
            "Presentation order describes endpoint comparability only and is not a "
            "forecast, wave decision, or trading instruction."
        ),
    }


def retrieval_policy(
    policy_id: str = DEFAULT_RETRIEVAL_POLICY_ID,
    policy_version: str = DEFAULT_RETRIEVAL_POLICY_VERSION,
) -> dict[str, Any]:
    """Return the only supported immutable retrieval-policy specification."""
    if policy_id != DEFAULT_RETRIEVAL_POLICY_ID:
        raise ValueError(
            f"Unsupported retrieval policy {policy_id!r}; supported: "
            f"{DEFAULT_RETRIEVAL_POLICY_ID}."
        )
    if policy_version != DEFAULT_RETRIEVAL_POLICY_VERSION:
        raise ValueError(
            f"Unsupported retrieval policy version {policy_version!r}; supported: "
            f"{DEFAULT_RETRIEVAL_POLICY_VERSION}."
        )
    payload = _policy_payload()
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _empty_summary() -> dict[str, int]:
    return {
        "comparable_count": 0,
        "missing_count": 0,
        "incomparable_count": 0,
        "omitted_count": 0,
        "within_tolerance_count": 0,
        "outside_tolerance_count": 0,
        "exact_categorical_match_count": 0,
        "categorical_mismatch_count": 0,
    }


def _categorical_match(dimension: Mapping[str, Any]) -> bool | None:
    if dimension.get("state") != "comparable" or dimension.get("data_type") not in {
        "category",
        "ordered_category",
    }:
        return None
    difference = dimension.get("difference")
    if not isinstance(difference, Mapping):
        return None
    exact = difference.get("exact_match")
    if isinstance(exact, bool):
        return exact
    compatibility = difference.get("compatibility_class")
    if compatibility is not None:
        return str(compatibility) == "exact_match"
    relationship = difference.get("relationship")
    if relationship in {"same", "different"}:
        return relationship == "same"
    return None


def _add_dimension(summary: dict[str, int], dimension: Mapping[str, Any]) -> None:
    state = str(dimension.get("state") or "unsupported_by_spec")
    if state == "comparable":
        summary["comparable_count"] += 1
    elif state in MISSING_STATES:
        summary["missing_count"] += 1
    elif state in INCOMPARABLE_STATES:
        summary["incomparable_count"] += 1
    else:
        summary["omitted_count"] += 1

    tolerance = dimension.get("tolerance_band_result")
    tolerance_status = (
        str(tolerance.get("status")) if isinstance(tolerance, Mapping) else ""
    )
    if tolerance_status == "within_tolerance":
        summary["within_tolerance_count"] += 1
    elif tolerance_status == "outside_tolerance":
        summary["outside_tolerance_count"] += 1

    categorical_match = _categorical_match(dimension)
    if categorical_match is True:
        summary["exact_categorical_match_count"] += 1
    elif categorical_match is False:
        summary["categorical_mismatch_count"] += 1


def _comparison_summary(comparison: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = comparison.get("dimensions")
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        raise ValueError("Every Phase 5A.3 comparison requires dimension records.")
    by_id: dict[str, Mapping[str, Any]] = {}
    group_summaries = {group: _empty_summary() for group in GROUP_PRECEDENCE}
    aggregate = _empty_summary()
    for raw_dimension in dimensions:
        if not isinstance(raw_dimension, Mapping):
            raise ValueError("A Phase 5A.3 dimension record is malformed.")
        dimension_id = str(raw_dimension.get("dimension_id") or "")
        group = str(raw_dimension.get("group") or "")
        if not dimension_id or dimension_id in by_id:
            raise ValueError("Phase 5A.3 dimension IDs must be present and unique.")
        if group not in group_summaries:
            raise ValueError(f"Unsupported Phase 5A.3 dimension group {group!r}.")
        by_id[dimension_id] = raw_dimension
        _add_dimension(group_summaries[group], raw_dimension)
        _add_dimension(aggregate, raw_dimension)

    expected = {item.dimension_id for item in DIMENSION_DEFINITIONS}
    if set(by_id) != expected:
        raise ValueError("The Phase 5A.3 comparison does not contain the full dimension set.")

    mandatory = _empty_summary()
    unavailable: list[dict[str, Any]] = []
    for dimension_id in MANDATORY_DIMENSIONS:
        item = by_id[dimension_id]
        _add_dimension(mandatory, item)
        if item.get("state") != "comparable":
            unavailable.append(
                {
                    "dimension_id": dimension_id,
                    "state": item.get("state"),
                    "reason": item.get("reason"),
                }
            )

    return {
        "mandatory_dimensions": {
            **mandatory,
            "required_count": len(MANDATORY_DIMENSIONS),
            "all_comparable": not unavailable,
            "unavailable": unavailable,
        },
        "groups": group_summaries,
        "aggregate": aggregate,
    }


def _retrieval_tier(
    comparison: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    level_result = comparison.get("comparison_level_result")
    if not isinstance(level_result, Mapping):
        raise ValueError("A comparison-level result is required for retrieval.")
    level = level_result.get("level")
    if level not in {1, 2, 3, 4}:
        raise ValueError("A Phase 5A.3 comparison has no valid comparison level.")
    compatibility = str(level_result.get("compatibility_class") or "")
    mandatory = summary["mandatory_dimensions"]
    aggregate = summary["aggregate"]
    all_mandatory = bool(mandatory["all_comparable"])
    no_feed_or_provenance_incomparability = aggregate["incomparable_count"] == 0

    if (
        level == 1
        and compatibility == "exact_match"
        and all_mandatory
        and no_feed_or_provenance_incomparability
    ):
        tier = 1
        reason = (
            "Level 1 exact structural compatibility, every mandatory structural "
            "dimension is comparable, and no feed/provenance dimension is incomparable."
        )
    elif (
        level in {1, 2, 3}
        and compatibility in {"exact_match", "compatible"}
        and all_mandatory
    ):
        tier = 2
        reason = (
            "Level 1-3 exact or compatible structure with every mandatory structural "
            "dimension comparable; at least one Tier 1 condition is not satisfied."
        )
    else:
        tier = 3
        reasons: list[str] = []
        if level == 4:
            reasons.append("cross-market Level 4 scope")
        if compatibility == "context_only":
            reasons.append("context-only structural compatibility")
        if not all_mandatory:
            reasons.append("unavailable mandatory structural evidence")
        if not reasons:
            reasons.append("broader Phase 5A.2-compatible context")
        reason = "Phase 5A.2 eligibility is preserved, but this is broader because of " + ", ".join(reasons) + "."
    return {
        "tier": tier,
        "label": TIER_LABELS[tier],
        "reason": reason,
        "conditions": {
            "comparison_level": level,
            "compatibility_class": compatibility,
            "all_mandatory_dimensions_comparable": all_mandatory,
            "no_feed_or_provenance_incomparability": no_feed_or_provenance_incomparability,
        },
    }


def _component(
    key: str, value: int | str, direction: str
) -> dict[str, Any]:
    return {"key": key, "value": value, "preferred_direction": direction}


def _ordering_components(
    comparison: Mapping[str, Any],
    summary: Mapping[str, Any],
    tier: Mapping[str, Any],
) -> list[dict[str, Any]]:
    level = int((comparison.get("comparison_level_result") or {}).get("level"))
    mandatory = summary["mandatory_dimensions"]
    components = [
        _component("retrieval_tier", int(tier["tier"]), "ascending"),
        _component("comparison_level", level, "ascending"),
        _component(
            "mandatory_incomparable_count",
            int(mandatory["incomparable_count"]),
            "ascending",
        ),
        _component(
            "mandatory_missing_count",
            int(mandatory["missing_count"]),
            "ascending",
        ),
        _component(
            "mandatory_omitted_count",
            int(mandatory["omitted_count"]),
            "ascending",
        ),
        _component(
            "mandatory_comparable_count",
            int(mandatory["comparable_count"]),
            "descending",
        ),
    ]
    for group in GROUP_PRECEDENCE:
        group_summary = summary["groups"][group]
        for key, direction in SUMMARY_KEY_PRECEDENCE:
            components.append(
                _component(
                    f"group.{group}.{key}",
                    int(group_summary[key]),
                    direction,
                )
            )
    aggregate = summary["aggregate"]
    for key, direction in SUMMARY_KEY_PRECEDENCE:
        components.append(
            _component(f"aggregate.{key}", int(aggregate[key]), direction)
        )
    historical = comparison.get("historical_experience_identifier") or {}
    components.append(
        _component(
            "experience_case_id",
            str(historical.get("experience_case_id") or ""),
            "ascending",
        )
    )
    return components


def _sort_tuple(components: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
    values: list[Any] = []
    for item in components:
        value = item.get("value")
        direction = item.get("preferred_direction")
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid retrieval ordering keys.")
        if isinstance(value, int):
            values.append(-value if direction == "descending" else value)
        elif isinstance(value, str) and direction == "ascending":
            values.append(value)
        else:
            raise ValueError("A retrieval ordering key has an unsupported value or direction.")
    return tuple(values)


def _first_difference(
    preferred: Sequence[Mapping[str, Any]],
    other: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    for left, right in zip(preferred, other):
        if left.get("key") != right.get("key"):
            raise ValueError("Retrieval ordering component layouts do not match.")
        if left.get("value") != right.get("value"):
            return {
                "key": left.get("key"),
                "preferred_value": left.get("value"),
                "other_value": right.get("value"),
                "preferred_direction": left.get("preferred_direction"),
            }
    raise ValueError("Retrieval ordering keys require a unique stable case ID.")


def _comparison_reference(comparison: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "comparison_id": comparison.get("comparison_id"),
        "schema_version": comparison.get("schema_version"),
        "calculation_version": comparison.get("calculation_version"),
        "comparison_specification_version": comparison.get(
            "comparison_specification_version"
        ),
        "comparison_specification_hash": comparison.get(
            "comparison_specification_hash"
        ),
        "content_hash": comparison.get("content_hash"),
    }


def _candidate_warnings(
    comparison: Mapping[str, Any],
    tier: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> list[dict[str, Any]]:
    warnings = [deepcopy(dict(item)) for item in comparison.get("warnings") or []]
    if tier["tier"] == 3:
        warnings.append(
            {
                "code": "broader_or_partially_available_analogue",
                "reason": tier["reason"],
                "effect": "presentation_tier_only",
            }
        )
    unavailable = summary["mandatory_dimensions"]["unavailable"]
    if unavailable:
        warnings.append(
            {
                "code": "mandatory_retrieval_dimensions_unavailable",
                "reason": (
                    "One or more policy-mandatory structural dimensions are not comparable."
                ),
                "dimensions": deepcopy(unavailable),
                "effect": "presentation_tier_and_lexicographic_order",
            }
        )
    return warnings


def _validate_comparison_set(
    comparison_set: Mapping[str, Any], policy: Mapping[str, Any]
) -> None:
    if comparison_set.get("schema_version") != ANALOGUE_COMPARISON_SCHEMA_VERSION:
        raise ValueError("Unsupported Phase 5A.3 comparison-set schema version.")
    if (
        comparison_set.get("calculation_version")
        != ANALOGUE_COMPARISON_CALCULATION_VERSION
    ):
        raise ValueError("Unsupported Phase 5A.3 comparison calculation version.")
    if (
        comparison_set.get("comparison_specification_version")
        != policy["comparison_specification_version"]
    ):
        raise ValueError("The comparison set and retrieval policy use different specifications.")
    expected_specification = comparison_specification(
        str(policy["comparison_specification_version"])
    )
    if (
        comparison_set.get("comparison_specification_hash")
        != expected_specification["content_hash"]
    ):
        raise ValueError("The Phase 5A.3 comparison specification hash is invalid.")
    if comparison_set.get("content_hash") != analogue_comparison_content_hash(
        comparison_set
    ):
        raise ValueError("The Phase 5A.3 comparison-set content hash is invalid.")
    filter_reference = comparison_set.get("phase5a2_filter_reference")
    if not isinstance(filter_reference, Mapping):
        raise ValueError("The Phase 5A.3 comparison set has no Phase 5A.2 reference.")
    expected_filter_versions = policy["phase5a2_filter_versions"]
    for key in (
        "schema_version",
        "calculation_version",
        "compatibility_matrix_version",
    ):
        if filter_reference.get(key) != expected_filter_versions[key]:
            raise ValueError(
                f"The Phase 5A.2 filter reference has an unsupported {key}."
            )

    seen: set[str] = set()
    current_id = str(
        (comparison_set.get("current_endpoint_identifier") or {}).get(
            "experience_case_id"
        )
        or ""
    )
    if not current_id:
        raise ValueError("The Phase 5A.3 comparison set has no current endpoint identifier.")
    for comparison in comparison_set.get("comparisons") or []:
        if not isinstance(comparison, Mapping):
            raise ValueError("A Phase 5A.3 comparison record is malformed.")
        if comparison.get("content_hash") != analogue_comparison_content_hash(
            comparison
        ):
            raise ValueError("A Phase 5A.3 comparison record has an invalid content hash.")
        comparison_current_id = str(
            (comparison.get("current_endpoint_identifier") or {}).get(
                "experience_case_id"
            )
            or ""
        )
        historical_id = str(
            (comparison.get("historical_experience_identifier") or {}).get(
                "experience_case_id"
            )
            or ""
        )
        if comparison_current_id != current_id or not historical_id:
            raise ValueError("A Phase 5A.3 comparison has inconsistent endpoint identity.")
        if historical_id in seen:
            raise ValueError("A historical experience case appears more than once.")
        seen.add(historical_id)
        filter_reference = comparison.get("filter_result_reference") or {}
        if filter_reference.get("eligible") is not True or filter_reference.get(
            "excluded"
        ) is True:
            raise ValueError("Retrieval may use only Phase 5A.2-eligible comparisons.")
        compatibility_rule = filter_reference.get("compatibility_rule") or {}
        role_rule = compatibility_rule.get("role") or {}
        family_rule = compatibility_rule.get("family") or {}
        if role_rule.get("matrix_version") != COMPATIBILITY_MATRIX_VERSION:
            raise ValueError("The role compatibility matrix version is unsupported.")
        if family_rule.get("matrix_version") != FAMILY_MATRIX_VERSION:
            raise ValueError("The family compatibility matrix version is unsupported.")
        cutoff = comparison.get("cutoff_validation") or {}
        if not all(
            isinstance(cutoff.get(side), Mapping)
            and cutoff[side].get("valid") is True
            for side in ("current", "historical")
        ):
            raise ValueError("A Phase 5A.3 comparison failed cutoff validation.")


def _unavailable_references(
    comparison_set: Mapping[str, Any],
    comparisons_above_level: Sequence[Mapping[str, Any]],
    maximum_comparison_level: int,
) -> list[dict[str, Any]]:
    unavailable = [
        deepcopy(dict(item))
        for item in comparison_set.get("eligible_but_omitted_by_requested_level")
        or []
        if isinstance(item, Mapping)
    ]
    for comparison in comparisons_above_level:
        historical = comparison.get("historical_experience_identifier") or {}
        level = (comparison.get("comparison_level_result") or {}).get("level")
        unavailable.append(
            {
                "experience_case_id": historical.get("experience_case_id"),
                "comparison_level": level,
                "rule": "RETRIEVAL-MAXIMUM-LEVEL",
                "reason": (
                    f"Candidate comparison Level {level} is above the requested "
                    f"maximum Level {maximum_comparison_level}."
                ),
            }
        )
    for warning in comparison_set.get("warnings") or []:
        if not isinstance(warning, Mapping):
            continue
        if warning.get("code") in {
            "eligible_candidate_payload_missing",
            "unknown_candidate_case",
        }:
            unavailable.append(deepcopy(dict(warning)))
    return sorted(
        unavailable,
        key=lambda item: (
            str(item.get("experience_case_id") or ""),
            str(item.get("rule") or item.get("code") or ""),
        ),
    )


def _empty_status(
    comparison_set: Mapping[str, Any], unavailable: Sequence[Mapping[str, Any]]
) -> tuple[str, str]:
    if unavailable:
        return (
            "unavailable_no_comparison_records",
            "No eligible comparison record is available at the requested level.",
        )
    excluded = [
        item
        for item in comparison_set.get("phase5a2_excluded_cases") or []
        if isinstance(item, Mapping)
    ]
    if not excluded or all(item.get("rule") == "HARD-SELF-MATCH" for item in excluded):
        return (
            "unavailable_empty_accepted_experience_pool",
            "No accepted historical experience cases are available after excluding the current case.",
        )
    return (
        "unavailable_no_eligible_analogues",
        "No accepted historical experience case survived the Phase 5A.2 structural filter.",
    )


def retrieve_analogues(
    comparison_set: Mapping[str, Any],
    *,
    policy_id: str = DEFAULT_RETRIEVAL_POLICY_ID,
    policy_version: str = DEFAULT_RETRIEVAL_POLICY_VERSION,
    maximum_comparison_level: int = 4,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> dict[str, Any]:
    """Order eligible Phase 5A.3 comparisons for deterministic human review."""
    policy = retrieval_policy(policy_id, policy_version)
    if maximum_comparison_level not in policy["allowed_phase5a2_comparison_levels"]:
        raise ValueError("maximum_comparison_level must be 1, 2, 3, or 4.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer.")
    if limit > int(policy["maximum_results"]["hard_maximum"]):
        raise ValueError(
            f"limit cannot exceed {policy['maximum_results']['hard_maximum']}."
        )
    _validate_comparison_set(comparison_set, policy)

    available_comparisons: list[Mapping[str, Any]] = []
    comparisons_above_level: list[Mapping[str, Any]] = []
    for raw in comparison_set.get("comparisons") or []:
        comparison = raw if isinstance(raw, Mapping) else {}
        level = int((comparison.get("comparison_level_result") or {}).get("level"))
        if level <= maximum_comparison_level:
            available_comparisons.append(comparison)
        else:
            comparisons_above_level.append(comparison)

    prepared: list[dict[str, Any]] = []
    for comparison in available_comparisons:
        summary = _comparison_summary(comparison)
        tier = _retrieval_tier(comparison, summary)
        components = _ordering_components(comparison, summary, tier)
        historical = comparison.get("historical_experience_identifier") or {}
        source_hashes = deepcopy(dict(comparison.get("source_hashes") or {}))
        prepared.append(
            {
                "experience_case_reference": deepcopy(dict(historical)),
                "experience_pool_gate": {
                    "accepted": True,
                    "source": "Phase 5A.2 eligible filter decision",
                },
                "phase5a2_filter_decision_reference": deepcopy(
                    dict(comparison.get("filter_result_reference") or {})
                ),
                "phase5a3_comparison_reference": _comparison_reference(comparison),
                "retrieval_tier": tier,
                "deterministic_ordering_keys": components,
                "comparison_summary": summary,
                "missing_dimensions": deepcopy(
                    list(comparison.get("missing_dimensions") or [])
                ),
                "incomparable_dimensions": deepcopy(
                    list(comparison.get("incomparable_dimensions") or [])
                ),
                "omitted_dimensions": deepcopy(
                    list(comparison.get("omitted_dimensions") or [])
                ),
                "comparison_evidence": {
                    "dimensions": deepcopy(list(comparison.get("dimensions") or [])),
                    "comparable_dimensions": deepcopy(
                        list(comparison.get("comparable_dimensions") or [])
                    ),
                    "dimension_state_counts": deepcopy(
                        dict(comparison.get("dimension_state_counts") or {})
                    ),
                    "group_state_counts": deepcopy(
                        dict(comparison.get("group_state_counts") or {})
                    ),
                },
                "provenance": deepcopy(dict(comparison.get("provenance") or {})),
                "cutoff_verification": deepcopy(
                    dict(comparison.get("cutoff_validation") or {})
                ),
                "source_hashes": source_hashes,
                "immutable_input_references": {
                    "current_endpoint_dna_hash": source_hashes.get(
                        "current_endpoint_dna"
                    ),
                    "current_phase3_fingerprint_hash": source_hashes.get(
                        "current_fingerprint"
                    ),
                    "current_phase4_endpoint_snapshot_hash": source_hashes.get(
                        "current_endpoint_snapshot"
                    ),
                    "historical_endpoint_dna_hash": source_hashes.get(
                        "historical_endpoint_dna"
                    ),
                    "historical_phase3_fingerprint_hash": source_hashes.get(
                        "historical_fingerprint"
                    ),
                    "historical_phase4_endpoint_snapshot_hash": source_hashes.get(
                        "historical_endpoint_snapshot"
                    ),
                },
                "warnings": _candidate_warnings(comparison, tier, summary),
                "_sort_tuple": _sort_tuple(components),
            }
        )

    prepared.sort(key=lambda item: item["_sort_tuple"])
    ordered = prepared[:limit]
    for index, item in enumerate(ordered):
        item["presentation_order"] = index + 1
        explanation: dict[str, Any] = {
            "policy": "First differing lexicographic key determines adjacent order."
        }
        if index == 0:
            explanation["ordered_after"] = None
        else:
            previous = ordered[index - 1]
            explanation["ordered_after"] = {
                "experience_case_id": previous["experience_case_reference"].get(
                    "experience_case_id"
                ),
                "first_differing_key": _first_difference(
                    previous["deterministic_ordering_keys"],
                    item["deterministic_ordering_keys"],
                ),
            }
        if index + 1 == len(ordered):
            explanation["ordered_before"] = None
        else:
            following = ordered[index + 1]
            explanation["ordered_before"] = {
                "experience_case_id": following["experience_case_reference"].get(
                    "experience_case_id"
                ),
                "first_differing_key": _first_difference(
                    item["deterministic_ordering_keys"],
                    following["deterministic_ordering_keys"],
                ),
            }
        item["ordering_explanation"] = explanation
        item.pop("_sort_tuple", None)

    unavailable = _unavailable_references(
        comparison_set, comparisons_above_level, maximum_comparison_level
    )
    excluded = [
        deepcopy(dict(item))
        for item in comparison_set.get("phase5a2_excluded_cases") or []
        if isinstance(item, Mapping)
    ]
    if ordered:
        status = "completed"
        availability_explanation = (
            "Eligible analogue comparisons were ordered by the declared deterministic policy."
        )
    else:
        status, availability_explanation = _empty_status(
            comparison_set, unavailable
        )

    cutoff_records = [item["cutoff_verification"] for item in ordered]
    payload: dict[str, Any] = {
        "schema_version": ANALOGUE_RETRIEVAL_SCHEMA_VERSION,
        "calculation_version": ANALOGUE_RETRIEVAL_CALCULATION_VERSION,
        "current_endpoint_identifier": deepcopy(
            dict(comparison_set.get("current_endpoint_identifier") or {})
        ),
        "retrieval_policy": {
            "policy_id": policy["policy_id"],
            "policy_version": policy["policy_version"],
            "content_hash": policy["content_hash"],
        },
        "comparison_specification_version": comparison_set.get(
            "comparison_specification_version"
        ),
        "comparison_specification_hash": comparison_set.get(
            "comparison_specification_hash"
        ),
        "filter_matrix_versions": deepcopy(policy["phase5a2_filter_versions"]),
        "requested_result_limit": limit,
        "maximum_comparison_level": maximum_comparison_level,
        "eligible_candidate_count": len(prepared),
        "excluded_candidate_count": len(excluded),
        "unavailable_candidate_count": len(unavailable),
        "presented_analogue_count": len(ordered),
        "not_presented_due_to_limit": max(0, len(prepared) - len(ordered)),
        "ordered_analogue_references": ordered,
        "excluded_candidate_references": excluded,
        "unavailable_candidate_references": unavailable,
        "provenance": {
            "source_phase5a3_comparison_set_hash": comparison_set.get(
                "content_hash"
            ),
            "source_phase5a2_filter_reference": deepcopy(
                dict(comparison_set.get("phase5a2_filter_reference") or {})
            ),
            "retrieval_policy_hash": policy["content_hash"],
            "input_scope": (
                "Phase 5A.2-eligible Phase 5A.3 endpoint comparison records only."
            ),
        },
        "cutoff_verification": {
            "all_presented_records_valid": (
                all(
                    record.get("current", {}).get("valid") is True
                    and record.get("historical", {}).get("valid") is True
                    for record in cutoff_records
                )
                if cutoff_records
                else None
            ),
            "presented_record_count": len(cutoff_records),
            "status": "verified" if cutoff_records else "unavailable_no_presented_records",
        },
        "warnings": deepcopy(list(comparison_set.get("warnings") or [])),
        "status": status,
        "availability_explanation": availability_explanation,
        "ordering_policy": (
            "Versioned lexicographic tiers and keys only; no synthetic overall score."
        ),
        "reviewed_outcome_policy": (
            "Reviewed outcomes were not loaded or used for filtering, comparison, tiering, "
            "ordering, selection, tie-breaking, or hashing."
        ),
        "interpretation_boundary": (
            "Presentation order describes endpoint comparability only."
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _display(value: Any, limit: int = 88) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        text = f"{value:.8g}"
    elif isinstance(value, (str, int, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_analogue_retrieval(
    result: Mapping[str, Any],
    *,
    explain: bool = False,
    show_provenance: bool = False,
    show_hashes: bool = False,
    show_ordering_keys: bool = False,
    include_comparison_details: bool = False,
) -> str:
    """Render the canonical retrieval result without changing its content."""
    current = result.get("current_endpoint_identifier") or {}
    policy = result.get("retrieval_policy") or {}
    lines = [
        "# Experience Analogue Retrieval",
        "",
        f"Current endpoint: {current.get('experience_case_id') or 'unknown'}",
        f"Policy: {policy.get('policy_id')} version {policy.get('policy_version')}",
        f"Comparison specification: {result.get('comparison_specification_version')}",
        f"Status: {result.get('status')}",
        f"Eligible candidates: {result.get('eligible_candidate_count', 0)}",
        f"Excluded candidates: {result.get('excluded_candidate_count', 0)}",
        f"Unavailable candidates: {result.get('unavailable_candidate_count', 0)}",
        f"Presented analogues: {result.get('presented_analogue_count', 0)}",
        "Ordering: explicit lexicographic tiers and keys; no synthetic overall score.",
        "Inputs: immutable endpoint-time comparison evidence only.",
    ]
    analogues = result.get("ordered_analogue_references") or []
    if not analogues:
        lines.extend(["", str(result.get("availability_explanation") or "No analogue is available.")])
    for analogue in analogues:
        reference = analogue.get("experience_case_reference") or {}
        tier = analogue.get("retrieval_tier") or {}
        summary = analogue.get("comparison_summary") or {}
        aggregate = summary.get("aggregate") or {}
        filter_reference = analogue.get("phase5a2_filter_decision_reference") or {}
        lines.extend(
            [
                "",
                f"## Order {analogue.get('presentation_order')}: {reference.get('experience_case_id') or 'unknown'}",
                "",
                f"Structural eligibility: Phase 5A.2 Level {filter_reference.get('comparison_level')} ({filter_reference.get('compatibility_class')})",
                f"Retrieval tier: {tier.get('label')}",
                f"Tier reason: {tier.get('reason')}",
                f"Comparable dimensions: {aggregate.get('comparable_count', 0)}",
                f"Within-tolerance dimensions: {aggregate.get('within_tolerance_count', 0)}",
                f"Exact categorical matches: {aggregate.get('exact_categorical_match_count', 0)}",
                f"Missing dimensions: {aggregate.get('missing_count', 0)}",
                f"Incomparable dimensions: {aggregate.get('incomparable_count', 0)}",
                f"Omitted/unsupported dimensions: {aggregate.get('omitted_count', 0)}",
            ]
        )
        warnings = analogue.get("warnings") or []
        if warnings:
            lines.append("Warnings: " + "; ".join(str(item.get("code")) for item in warnings))
        if explain:
            ordering = analogue.get("ordering_explanation") or {}
            lines.append("Ordering explanation: " + _display(ordering, 500))
            mandatory = summary.get("mandatory_dimensions") or {}
            lines.append(
                "Mandatory evidence: "
                + (
                    "all comparable"
                    if mandatory.get("all_comparable")
                    else _display(mandatory.get("unavailable"), 500)
                )
            )
        if show_ordering_keys:
            lines.extend(["", "| Ordering key | Value | Preferred |", "|---|---:|---|"])
            for item in analogue.get("deterministic_ordering_keys") or []:
                lines.append(
                    f"| {item.get('key')} | {_display(item.get('value'))} | {item.get('preferred_direction')} |"
                )
        if include_comparison_details:
            lines.extend(
                [
                    "",
                    "| Dimension | State | Current | Historical | Difference |",
                    "|---|---|---:|---:|---|",
                ]
            )
            evidence = analogue.get("comparison_evidence") or {}
            for dimension in evidence.get("dimensions") or []:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(dimension.get("dimension_id")),
                            str(dimension.get("state")),
                            _display(dimension.get("current_value")),
                            _display(dimension.get("historical_value")),
                            _display(dimension.get("difference")),
                        ]
                    )
                    + " |"
                )
        if show_provenance:
            lines.append("Provenance: " + _display(analogue.get("provenance"), 500))
            lines.append(
                "Cutoff verification: "
                + _display(analogue.get("cutoff_verification"), 500)
            )
        if show_hashes:
            comparison_reference = analogue.get("phase5a3_comparison_reference") or {}
            lines.append(
                f"Phase 5A.3 comparison hash: {comparison_reference.get('content_hash')}"
            )
            lines.append("Source hashes: " + _display(analogue.get("source_hashes"), 500))
    if show_hashes:
        lines.extend(
            [
                "",
                f"Retrieval policy hash: {policy.get('content_hash')}",
                f"Retrieval result hash: {result.get('content_hash')}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_retrieval_policy(policy: Mapping[str, Any]) -> str:
    """Render a compact audit view of the retrieval policy."""
    lines = [
        "# Experience Retrieval Policy",
        "",
        f"Policy: {policy.get('policy_id')}",
        f"Version: {policy.get('policy_version')}",
        f"Schema: {policy.get('schema_version')}",
        "Ordering model: lexicographic",
        "Overall score: disabled",
        "Diversity: disabled and deferred",
        "",
        "## Tiers",
    ]
    for tier in policy.get("retrieval_tiers") or []:
        lines.append(f"- {tier.get('label')}: {' '.join(tier.get('requirements') or [])}")
    lines.extend(["", "## Group Precedence"])
    for index, group in enumerate(policy.get("grouping_precedence") or [], start=1):
        lines.append(f"{index}. {group}")
    lines.extend(
        [
            "",
            f"Final tie-breaker: {(policy.get('ordering_model') or {}).get('final_tie_breaker')}",
            f"Policy hash: {policy.get('content_hash')}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
