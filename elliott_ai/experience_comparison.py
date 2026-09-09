"""Deterministic structural filtering for reviewed Elliott experiences.

Phase 5A.2 answers only whether two endpoint-DNA records are structurally
comparable.  It deliberately contains no distance, score, weight, ranking, or
statistical selection logic.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import timezone
from typing import Any, Iterable, Mapping, Sequence

from .correction_semantics import combination_variant, structure_family
from .experience import (
    DNA_GROUP_NAMES,
    EXPERIENCE_SCHEMA_VERSION,
    PATTERN_DNA_CALCULATION_VERSION,
    PATTERN_DNA_SCHEMA_VERSION,
    pattern_dna_content_hash,
)
from .market_data import parse_date
from .schema import STANDARD_DEGREES


COMPARISON_SCHEMA_VERSION = "experience-comparison-1.0.0"
COMPARISON_CALCULATION_VERSION = "experience-comparison-filter-1.0.0"
ROLE_CLASS_VERSION = "structural-role-class-1.0.0"
COMPATIBILITY_MATRIX_VERSION = "structural-compatibility-matrix-1.0.0"
FAMILY_MATRIX_VERSION = "structural-family-matrix-1.0.0"

COMPATIBILITY_CLASSES = (
    "exact_match",
    "compatible",
    "context_only",
    "incompatible",
)
DEGREE_RELATIONSHIPS = ("same_degree", "adjacent_degree", "far_degree")

MANDATORY_ENDPOINT_GROUPS = frozenset(
    {"source_identity", "structural_context"}
)
OPTIONAL_INDICATOR_GROUPS = (
    "momentum",
    "volume",
    "volatility",
    "optional_rsi",
)
ENDPOINT_FORBIDDEN_KEYS = frozenset(
    {
        "confirmation_events",
        "resolved_outcome",
        "reviewed_outcome",
        "post_terminal_events",
        "displacement",
        "five_away_candidate",
        "origin_hold",
        "origin_invalidation",
        "x2_z_continuation",
    }
)


@dataclass(frozen=True)
class ComparisonConfig:
    """Conservative structural-filter switches with no numerical weights."""

    same_degree_only: bool = False
    allow_adjacent_degree: bool = True
    allow_far_degree: bool = False
    allow_cross_market: bool = False
    allow_context_family: bool = False

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | None = None
    ) -> "ComparisonConfig":
        raw = dict(value or {})
        return cls(
            same_degree_only=bool(raw.get("same_degree_only", False)),
            allow_adjacent_degree=bool(raw.get("allow_adjacent_degree", True)),
            allow_far_degree=bool(raw.get("allow_far_degree", False)),
            allow_cross_market=bool(raw.get("allow_cross_market", False)),
            allow_context_family=bool(raw.get("allow_context_family", False)),
        )


def _role_definition(
    role_class: str,
    name: str,
    family: str,
    position: str,
    *,
    internal_family: str | None = None,
) -> dict[str, Any]:
    return {
        "role_class": role_class,
        "name": name,
        "family": family,
        "position": position,
        "internal_family": internal_family,
        "version": ROLE_CLASS_VERSION,
    }


def _build_role_definitions() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for position in ("1", "2", "3", "4", "5"):
        role = f"impulse.wave_{position}"
        result[role] = _role_definition(
            role, f"Impulse Wave {position}", "impulse", position
        )
    for family, display, positions in (
        ("zigzag", "Zigzag", ("A", "B", "C")),
        ("flat", "Flat", ("A", "B", "C")),
        ("triangle", "Triangle", ("A", "B", "C", "D", "E")),
        ("double_three", "Double Three", ("W", "X", "Y")),
        ("triple_three", "Triple Three", ("W", "X", "Y", "X2", "Z")),
    ):
        for position in positions:
            role = f"{family}.{position.lower()}"
            result[role] = _role_definition(
                role, f"{display} {position}", family, position
            )
    for internal in (
        "impulse",
        "diagonal",
        "zigzag",
        "flat",
        "triangle",
        "double_three",
        "triple_three",
    ):
        role = f"corrective_a.{internal}"
        result[role] = _role_definition(
            role,
            f"Corrective Wave A ({internal.replace('_', ' ')})",
            "corrective_a",
            "A",
            internal_family=internal,
        )
    return result


ROLE_CLASS_DEFINITIONS = _build_role_definitions()
ROLE_CLASSES = tuple(ROLE_CLASS_DEFINITIONS)

FAMILY_CLASSES = (
    "impulse",
    "diagonal",
    "zigzag",
    "flat",
    "triangle",
    "double_three",
    "triple_three",
    "corrective_a",
    "unknown",
)


def _normalize_family(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in FAMILY_CLASSES:
        return text
    family = structure_family(value)
    if family == "combination":
        return (
            "triple_three"
            if combination_variant(value) == "triple"
            else "double_three"
        )
    return family if family in FAMILY_CLASSES else "unknown"


def _normalize_position(value: Any) -> str:
    return str(value or "").strip().replace("(", "").replace(")", "").upper()


def classify_structural_role(
    *,
    parent_family: Any,
    position: Any,
    internal_family: Any = None,
    declared_role_class: Any = None,
    role_semantics: Any = None,
) -> dict[str, Any]:
    """Resolve a role from family, position, and actual internal structure."""
    declared = str(declared_role_class or "").strip().lower()
    family = _normalize_family(parent_family)
    normalized_position = _normalize_position(position)
    internal = _normalize_family(internal_family)
    if declared:
        definition = ROLE_CLASS_DEFINITIONS.get(declared)
        if definition is None:
            return {
                "status": "unavailable",
                "role_class": None,
                "family_class": family,
                "position": normalized_position,
                "internal_family": internal,
                "reason": f"Unsupported declared role class {declared!r}.",
                "version": ROLE_CLASS_VERSION,
            }
        return {
            "status": "recognized",
            **deepcopy(definition),
            "family_class": family if family != "unknown" else definition["family"],
            "classification_source": "explicit_endpoint_role_class",
        }

    semantics = str(role_semantics or "").strip().lower().replace("-", "_")
    if family == "corrective_a" or semantics in {
        "corrective_a",
        "corrective_wave_a",
        "wave_a_candidate",
    }:
        refined = internal if internal in FAMILY_CLASSES else "unknown"
        role = f"corrective_a.{refined}"
    elif family in {"impulse", "diagonal"} and normalized_position in {
        "1",
        "2",
        "3",
        "4",
        "5",
    }:
        role = f"impulse.wave_{normalized_position}"
    elif family in {"zigzag", "flat"} and normalized_position in {"A", "B", "C"}:
        role = f"{family}.{normalized_position.lower()}"
    elif family == "triangle" and normalized_position in {"A", "B", "C", "D", "E"}:
        role = f"triangle.{normalized_position.lower()}"
    elif family == "double_three" and normalized_position in {"W", "X", "Y"}:
        role = f"double_three.{normalized_position.lower()}"
    elif family == "triple_three" and normalized_position in {"W", "X", "Y", "X2", "Z"}:
        role = f"triple_three.{normalized_position.lower()}"
    elif normalized_position == "A":
        refined = internal if internal in FAMILY_CLASSES else "unknown"
        role = f"corrective_a.{refined}"
        family = "corrective_a"
    else:
        role = ""
    definition = ROLE_CLASS_DEFINITIONS.get(role)
    if definition is None:
        return {
            "status": "unavailable",
            "role_class": None,
            "family_class": family,
            "position": normalized_position,
            "internal_family": internal,
            "reason": (
                "Family, position, and internal structure do not resolve to a supported "
                "structural role class."
            ),
            "version": ROLE_CLASS_VERSION,
        }
    return {
        "status": "recognized",
        **deepcopy(definition),
        "family_class": family,
        "classification_source": "endpoint_family_position_and_internal_structure",
    }


def _matrix_rule(
    compatibility_class: str, rule_id: str, reason: str
) -> dict[str, str]:
    return {
        "compatibility_class": compatibility_class,
        "rule_id": rule_id,
        "reason": reason,
    }


def _role_pair_rule(left: str, right: str) -> dict[str, str]:
    if left == right:
        return _matrix_rule(
            "exact_match", "ROLE-EXACT", "Both endpoints have the same explicit role class."
        )
    a = ROLE_CLASS_DEFINITIONS[left]
    b = ROLE_CLASS_DEFINITIONS[right]
    families = {str(a["family"]), str(b["family"])}
    positions = {str(a["position"]), str(b["position"])}

    if "triangle" in families:
        return _matrix_rule(
            "incompatible",
            "ROLE-TRIANGLE-ISOLATION",
            "Triangle legs compare only with the exact same triangle leg role.",
        )
    if families == {"double_three", "triple_three"}:
        if a["position"] == b["position"] and a["position"] in {"W", "X", "Y"}:
            return _matrix_rule(
                "compatible",
                "ROLE-COMBINATION-SAME-POSITION",
                "The same W, X, or Y position is comparable across double and triple threes.",
            )
        return _matrix_rule(
            "incompatible",
            "ROLE-COMBINATION-POSITION-MISMATCH",
            "Combination positions differ; the first X is never reinterpreted as X2.",
        )
    if families == {"zigzag", "flat"}:
        if positions == {"C"}:
            return _matrix_rule(
                "compatible",
                "ROLE-TERMINAL-C",
                "Zigzag C and Flat C are terminal motive or allowed-diagonal roles.",
            )
        if len(positions) == 1 and next(iter(positions)) in {"A", "B"}:
            return _matrix_rule(
                "context_only",
                "ROLE-CORRECTION-SAME-LETTER-CONTEXT",
                "The same letter occupies materially different structural families and is context only.",
            )
        return _matrix_rule(
            "incompatible",
            "ROLE-CORRECTION-POSITION-MISMATCH",
            "The correction positions differ across zigzag and flat families.",
        )
    if a["family"] == b["family"] == "impulse":
        motive_positions = {"1", "3", "5"}
        corrective_positions = {"2", "4"}
        if set(positions).issubset(motive_positions) or set(positions).issubset(
            corrective_positions
        ):
            return _matrix_rule(
                "context_only",
                "ROLE-IMPULSE-SIBLING-CONTEXT",
                "Different impulse positions may provide context but are not the same role.",
            )
        return _matrix_rule(
            "incompatible",
            "ROLE-IMPULSE-MOTIVE-CORRECTIVE-MISMATCH",
            "Motive and corrective positions inside an impulse are structurally different.",
        )
    if "corrective_a" in families:
        corrective = a if a["family"] == "corrective_a" else b
        other = b if corrective is a else a
        internal = str(corrective.get("internal_family") or "unknown")
        if other["role_class"] == "impulse.wave_1" and internal in {
            "impulse",
            "diagonal",
        }:
            return _matrix_rule(
                "context_only",
                "ROLE-WAVE1-VS-CORRECTIVE-A",
                "An initial motive move remains Wave 1 versus corrective A ambiguous at its endpoint.",
            )
        if other["role_class"] == "zigzag.a" and internal in {
            "impulse",
            "diagonal",
        }:
            return _matrix_rule(
                "compatible",
                "ROLE-ZIGZAG-A-INTERNAL-MOTIVE",
                "Corrective A with motive internals is compatible with Zigzag A.",
            )
        if other["role_class"] == "flat.a" and internal in {
            "zigzag",
            "flat",
            "double_three",
            "triple_three",
        }:
            return _matrix_rule(
                "compatible",
                "ROLE-FLAT-A-INTERNAL-CORRECTIVE",
                "Corrective A with corrective internals is compatible with Flat A.",
            )
        if a["family"] == b["family"] == "corrective_a":
            if {str(a.get("internal_family")), str(b.get("internal_family"))} == {
                "impulse",
                "diagonal",
            }:
                return _matrix_rule(
                    "compatible",
                    "ROLE-CORRECTIVE-A-MOTIVE-VARIANTS",
                    "Impulse and diagonal internals are compatible motive variants for corrective A.",
                )
            return _matrix_rule(
                "context_only",
                "ROLE-CORRECTIVE-A-INTERNAL-CONTEXT",
                "Corrective A roles with different actual internals are context only.",
            )
        return _matrix_rule(
            "incompatible",
            "ROLE-CORRECTIVE-A-ROLE-MISMATCH",
            "The refined corrective-A internals do not match the other structural role.",
        )
    if a["family"] == b["family"] and a["family"] in {"zigzag", "flat"}:
        if positions.issubset({"A", "C"}):
            return _matrix_rule(
                "context_only",
                "ROLE-SAME-CORRECTION-MOTIVE-CONTEXT",
                "Different motive-capable positions in one correction family are context only.",
            )
    return _matrix_rule(
        "incompatible",
        "ROLE-NO-STRUCTURAL-RULE",
        "No approved structural rule makes these explicit roles comparable.",
    )


def _family_pair_rule(left: str, right: str) -> dict[str, str]:
    if left == right and left != "unknown":
        return _matrix_rule(
            "exact_match", "FAMILY-EXACT", "Both endpoints have the same structural family."
        )
    pair = {left, right}
    if "unknown" in pair:
        return _matrix_rule(
            "incompatible",
            "FAMILY-UNKNOWN",
            "Unknown family cannot pass a structural comparison filter.",
        )
    if pair == {"impulse", "diagonal"}:
        return _matrix_rule(
            "compatible",
            "FAMILY-MOTIVE-VARIANTS",
            "Impulse and diagonal are compatible motive families when the role permits it.",
        )
    if pair == {"double_three", "triple_three"}:
        return _matrix_rule(
            "compatible",
            "FAMILY-COMBINATION-VARIANTS",
            "Double and triple threes are compatible combination families at shared positions.",
        )
    if pair == {"zigzag", "flat"}:
        return _matrix_rule(
            "context_only",
            "FAMILY-SIMPLE-CORRECTION-CONTEXT",
            "Flat and zigzag are different simple corrections and are context only unless a role rule is more specific.",
        )
    if "triangle" in pair:
        return _matrix_rule(
            "incompatible",
            "FAMILY-TRIANGLE-ISOLATION",
            "Triangle structure is not interchangeable with a non-triangle family.",
        )
    corrective_families = {"zigzag", "flat", "double_three", "triple_three"}
    if pair.issubset(corrective_families):
        return _matrix_rule(
            "context_only",
            "FAMILY-CORRECTION-CONTEXT",
            "Different corrective families can provide context only.",
        )
    if "corrective_a" in pair:
        return _matrix_rule(
            "context_only",
            "FAMILY-CORRECTIVE-A-CONTEXT",
            "Corrective A requires its refined internal role before broader family comparison.",
        )
    return _matrix_rule(
        "incompatible",
        "FAMILY-MOTIVE-CORRECTIVE-MISMATCH",
        "Motive and corrective structural families are incompatible.",
    )


def _build_matrix(
    classes: Sequence[str], rule_builder: Any
) -> dict[str, dict[str, dict[str, str]]]:
    return {
        left: {right: rule_builder(left, right) for right in classes}
        for left in classes
    }


ROLE_COMPATIBILITY_MATRIX = _build_matrix(ROLE_CLASSES, _role_pair_rule)
FAMILY_COMPATIBILITY_MATRIX = _build_matrix(FAMILY_CLASSES, _family_pair_rule)


def role_compatibility(left: str, right: str) -> dict[str, str]:
    if left not in ROLE_COMPATIBILITY_MATRIX or right not in ROLE_CLASS_DEFINITIONS:
        return _matrix_rule(
            "incompatible", "ROLE-UNSUPPORTED", "One or both role classes are unsupported."
        )
    rule = deepcopy(ROLE_COMPATIBILITY_MATRIX[left][right])
    rule["matrix_location"] = f"role[{left}][{right}]"
    rule["matrix_version"] = COMPATIBILITY_MATRIX_VERSION
    return rule


def family_compatibility(left: str, right: str) -> dict[str, str]:
    if left not in FAMILY_COMPATIBILITY_MATRIX or right not in FAMILY_CLASSES:
        return _matrix_rule(
            "incompatible", "FAMILY-UNSUPPORTED", "One or both family classes are unsupported."
        )
    rule = deepcopy(FAMILY_COMPATIBILITY_MATRIX[left][right])
    rule["matrix_location"] = f"family[{left}][{right}]"
    rule["matrix_version"] = FAMILY_MATRIX_VERSION
    return rule


_DEGREE_INDEX = {
    degree.lower(): index
    for index, degree in enumerate(STANDARD_DEGREES)
    if degree != "Unassigned"
}


def degree_relationship(left: Any, right: Any) -> dict[str, Any]:
    left_name = str(left or "").strip()
    right_name = str(right or "").strip()
    left_index = _DEGREE_INDEX.get(left_name.lower())
    right_index = _DEGREE_INDEX.get(right_name.lower())
    if left_index is None or right_index is None:
        relationship = "far_degree"
        distance = None
    else:
        distance = abs(left_index - right_index)
        relationship = (
            "same_degree"
            if distance == 0
            else "adjacent_degree"
            if distance == 1
            else "far_degree"
        )
    return {
        "relationship": relationship,
        "distance": distance,
        "current_degree": left_name,
        "historical_degree": right_name,
        "degree_order": list(STANDARD_DEGREES),
    }


def _all_keys(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            result.add(str(key))
            result.update(_all_keys(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            result.update(_all_keys(child))
    return result


def _not_after(left: Any, right: Any) -> bool:
    first = parse_date(left)
    second = parse_date(right)
    if first is None or second is None:
        return False
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if second.tzinfo is None:
        second = second.replace(tzinfo=timezone.utc)
    return first.astimezone(timezone.utc) <= second.astimezone(timezone.utc)


def validate_endpoint_dna(dna: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate endpoint-only integrity without reading later DNA layers."""
    reasons: list[dict[str, str]] = []
    missing_groups: list[str] = []
    if not isinstance(dna, Mapping):
        return {
            "valid": False,
            "reasons": [
                {
                    "rule": "DNA-MISSING",
                    "reason": "Mandatory endpoint DNA is missing.",
                    "location": "endpoint_dna",
                }
            ],
            "missing_groups": list(MANDATORY_ENDPOINT_GROUPS),
        }
    if dna.get("dna_kind") != "endpoint":
        reasons.append(
            {
                "rule": "DNA-ENDPOINT-ONLY",
                "reason": "Structural filtering accepts endpoint DNA only.",
                "location": "endpoint_dna.dna_kind",
            }
        )
    if dna.get("schema_version") != PATTERN_DNA_SCHEMA_VERSION or dna.get(
        "calculation_version"
    ) != PATTERN_DNA_CALCULATION_VERSION:
        reasons.append(
            {
                "rule": "DNA-UNSUPPORTED-SCHEMA",
                "reason": "Endpoint DNA schema or calculation version is unsupported.",
                "location": "endpoint_dna.schema_version",
            }
        )
    if dna.get("content_hash") != pattern_dna_content_hash(dna):
        reasons.append(
            {
                "rule": "DNA-INVALID-HASH",
                "reason": "Endpoint DNA content hash does not match its canonical payload.",
                "location": "endpoint_dna.content_hash",
            }
        )
    if not str(dna.get("source_fingerprint_hash") or ""):
        reasons.append(
            {
                "rule": "DNA-MISSING-SOURCE-HASH",
                "reason": "Endpoint DNA has no source fingerprint hash.",
                "location": "endpoint_dna.source_fingerprint_hash",
            }
        )
    groups = dna.get("groups") if isinstance(dna.get("groups"), Mapping) else {}
    for name in DNA_GROUP_NAMES:
        group = groups.get(name)
        if not isinstance(group, Mapping) or group.get("status") == "unavailable":
            missing_groups.append(name)
    for name in MANDATORY_ENDPOINT_GROUPS:
        group = groups.get(name)
        if not isinstance(group, Mapping) or group.get("status") not in {
            "available",
            "partial",
        } or not isinstance(group.get("values"), Mapping):
            reasons.append(
                {
                    "rule": "DNA-MISSING-MANDATORY-GROUP",
                    "reason": f"Mandatory endpoint group {name!r} is unavailable or malformed.",
                    "location": f"endpoint_dna.groups.{name}",
                }
            )
    forbidden = sorted(_all_keys(dna).intersection(ENDPOINT_FORBIDDEN_KEYS))
    if forbidden:
        reasons.append(
            {
                "rule": "DNA-LATER-EVIDENCE-LEAKAGE",
                "reason": f"Endpoint DNA contains later-timing keys: {forbidden!r}.",
                "location": "endpoint_dna",
            }
        )
    structural_values = (
        groups.get("structural_context", {}).get("values", {})
        if isinstance(groups.get("structural_context"), Mapping)
        else {}
    )
    source_values = (
        groups.get("source_identity", {}).get("values", {})
        if isinstance(groups.get("source_identity"), Mapping)
        else {}
    )
    identity = source_values.get("identity") if isinstance(source_values, Mapping) and isinstance(source_values.get("identity"), Mapping) else {}
    endpoint_timestamp = identity.get("end_timestamp") or structural_values.get(
        "endpoint_timestamp"
    )
    cutoff = dna.get("cutoff")
    if not cutoff or not endpoint_timestamp or not _not_after(cutoff, endpoint_timestamp):
        reasons.append(
            {
                "rule": "DNA-ENDPOINT-CUTOFF-LEAKAGE",
                "reason": "Endpoint DNA cutoff is missing or occurs after the endpoint timestamp.",
                "location": "endpoint_dna.cutoff",
            }
        )
    for name, group in groups.items():
        if not isinstance(group, Mapping):
            continue
        group_cutoff = group.get("cutoff")
        if group_cutoff and cutoff and not _not_after(group_cutoff, cutoff):
            reasons.append(
                {
                    "rule": "DNA-GROUP-CUTOFF-LEAKAGE",
                    "reason": f"Feature group {name!r} has a cutoff later than endpoint DNA.",
                    "location": f"endpoint_dna.groups.{name}.cutoff",
                }
            )
    return {
        "valid": not reasons,
        "reasons": reasons,
        "missing_groups": sorted(set(missing_groups)),
    }


def endpoint_descriptor(dna: Mapping[str, Any]) -> dict[str, Any]:
    groups = dna.get("groups") if isinstance(dna.get("groups"), Mapping) else {}
    source_group = groups.get("source_identity") if isinstance(groups.get("source_identity"), Mapping) else {}
    source_values = source_group.get("values") if isinstance(source_group.get("values"), Mapping) else {}
    source = source_values.get("source") if isinstance(source_values.get("source"), Mapping) else {}
    identity = source_values.get("identity") if isinstance(source_values.get("identity"), Mapping) else {}
    structural_group = groups.get("structural_context") if isinstance(groups.get("structural_context"), Mapping) else {}
    structural = structural_group.get("values") if isinstance(structural_group.get("values"), Mapping) else {}
    market_group = groups.get("market_context") if isinstance(groups.get("market_context"), Mapping) else {}
    market = market_group.get("values") if isinstance(market_group.get("values"), Mapping) else {}
    volatility_group = groups.get("volatility") if isinstance(groups.get("volatility"), Mapping) else {}
    volatility = volatility_group.get("values") if isinstance(volatility_group.get("values"), Mapping) else {}
    parent_family = structural.get("parent_pattern_family") or identity.get(
        "parent_pattern_family"
    )
    position = structural.get("candidate_role") or identity.get("wave_label")
    internal_family = structural.get("internal_structure_family")
    role = classify_structural_role(
        parent_family=parent_family,
        position=position,
        internal_family=internal_family,
        declared_role_class=structural.get("role_class"),
        role_semantics=structural.get("role_semantics"),
    )
    return {
        "role": role,
        "family_class": role.get("family_class") or _normalize_family(parent_family),
        "position": _normalize_position(position),
        "elliott_degree": structural.get("elliott_degree") or identity.get("wave_degree"),
        "source": {
            "provider": str(source.get("provider") or "").strip(),
            "feed_identity": str(source.get("feed_identity") or "").strip(),
            "timeframe": str(source.get("timeframe") or identity.get("timeframe") or "").strip(),
            "venue": str(source.get("venue") or market.get("venue") or "").strip(),
            "market_type": str(source.get("market_type") or market.get("market_type") or "").strip(),
            "quote_currency": str(source.get("quote_currency") or market.get("quote_currency") or "").strip(),
            "derivative_type": str(source.get("derivative_type") or market.get("derivative_type") or "").strip(),
            "mandatory_provenance_status": str(source.get("mandatory_provenance_status") or "compatible").strip().lower(),
        },
        "regime": market.get("broader_regime") or market.get("regime"),
        "liquidity": market.get("liquidity") or source.get("liquidity"),
        "volatility_state": volatility.get("volatility_state"),
        "group_statuses": {
            name: (
                str(group.get("status") or "unavailable")
                if isinstance(group, Mapping)
                else "unavailable"
            )
            for name, group in groups.items()
        },
    }


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "experience_case_id": candidate.get("experience_case_id"),
        "market_episode_id": candidate.get("market_episode_id"),
        "case_version": candidate.get("case_version"),
    }


def _excluded(
    candidate: Mapping[str, Any],
    *,
    rule: str,
    reason: str,
    location: str,
    missing_groups: Iterable[str] = (),
    incomparable_groups: Iterable[str] = (),
    compatibility_class: str | None = None,
    compatibility_rule: Mapping[str, Any] | None = None,
    soft_penalties: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        **_candidate_identity(candidate),
        "eligible": False,
        "excluded": True,
        "reason": reason,
        "comparison_reason": reason,
        "rule": rule,
        "matrix_location": location,
        "comparison_level": None,
        "compatibility_class": compatibility_class,
        "compatibility_rule": deepcopy(dict(compatibility_rule or {})),
        "missing_groups": sorted(set(missing_groups)),
        "incomparable_groups": sorted(set(incomparable_groups)),
        "soft_penalties": [deepcopy(dict(item)) for item in soft_penalties],
        "comparison_scope": None,
    }


def _soft_penalty(code: str, reason: str) -> dict[str, str]:
    return {"code": code, "reason": reason, "effect": "metadata_only_no_score"}


def _optional_missing(
    current: Mapping[str, Any], historical: Mapping[str, Any]
) -> list[str]:
    missing: list[str] = []
    for name in OPTIONAL_INDICATOR_GROUPS:
        if current.get("group_statuses", {}).get(name) == "unavailable" or historical.get(
            "group_statuses", {}
        ).get(name) == "unavailable":
            missing.append(name)
    return missing


def _soft_metadata(
    current: Mapping[str, Any],
    historical: Mapping[str, Any],
    degree: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[str], list[str], bool]:
    penalties: list[dict[str, str]] = []
    incomparable: list[str] = []
    missing = _optional_missing(current, historical)
    if degree["relationship"] == "adjacent_degree":
        penalties.append(
            _soft_penalty("adjacent_degree", "The historical endpoint is one Elliott degree away.")
        )
    current_source = current["source"]
    historical_source = historical["source"]
    cross_market = bool(
        current_source.get("market_type")
        and historical_source.get("market_type")
        and current_source.get("market_type") != historical_source.get("market_type")
    )
    if cross_market:
        penalties.append(
            _soft_penalty("cross_market", "Market types differ; only normalized analogue fields remain comparable.")
        )
        incomparable.extend(
            ["price_shape.raw_price", "pivot_path.raw_price", "volume.raw"]
        )
    if current_source.get("feed_identity") != historical_source.get("feed_identity"):
        penalties.append(
            _soft_penalty("different_feed", "Feed identities differ; feed-bound measurements are incomparable.")
        )
        incomparable.extend(["volume.raw", "volume_profile", "funding", "basis"])
    if current_source.get("timeframe") != historical_source.get("timeframe"):
        incomparable.extend(
            ["momentum.raw_ewo", "momentum.raw_macd", "duration.raw_candles"]
        )
    comparisons = (
        ("different_regime", "regime", "Market regimes differ."),
        ("different_volatility", "volatility_state", "Volatility states differ."),
        ("different_liquidity", "liquidity", "Liquidity contexts differ."),
    )
    for code, field, reason in comparisons:
        left = current.get(field)
        right = historical.get(field)
        if left not in (None, "") and right not in (None, "") and left != right:
            penalties.append(_soft_penalty(code, reason))
    source_comparisons = (
        ("different_venue", "venue", "Trading venues differ."),
        ("different_derivative_type", "derivative_type", "Derivative types differ."),
        ("different_quote_currency", "quote_currency", "Quote currencies differ."),
    )
    for code, field, reason in source_comparisons:
        left = current_source.get(field)
        right = historical_source.get(field)
        if left and right and left != right:
            penalties.append(_soft_penalty(code, reason))
    if missing:
        penalties.append(
            _soft_penalty(
                "missing_optional_indicators",
                f"Optional endpoint groups are unavailable in one or both cases: {sorted(missing)!r}.",
            )
        )
    return penalties, sorted(set(missing)), sorted(set(incomparable)), cross_market


def _combined_compatibility(
    role_rule: Mapping[str, Any], family_rule: Mapping[str, Any]
) -> str:
    role_class = str(role_rule.get("compatibility_class"))
    family_class = str(family_rule.get("compatibility_class"))
    if "incompatible" in {role_class, family_class}:
        return "incompatible"
    if role_class == "context_only":
        return "context_only"
    if role_class == "compatible":
        return "compatible"
    if family_class == "context_only":
        return "context_only"
    if family_class == "compatible":
        return "compatible"
    return "exact_match"


def _comparison_level(
    *,
    role_class: str,
    family_class: str,
    degree_relation: str,
    cross_market: bool,
) -> int:
    if cross_market:
        return 4
    if role_class == "exact_match" and family_class == "exact_match":
        if degree_relation == "same_degree":
            return 1
        if degree_relation == "adjacent_degree":
            return 2
    return 3


def filter_structurally_comparable_cases(
    current_candidate: Mapping[str, Any],
    historical_cases: Iterable[Mapping[str, Any]],
    *,
    config: ComparisonConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structurally eligible and excluded cases in stable identity order."""
    settings = (
        config
        if isinstance(config, ComparisonConfig)
        else ComparisonConfig.from_mapping(config)
    )
    current_dna = current_candidate.get("endpoint_dna")
    current_validation = validate_endpoint_dna(
        current_dna if isinstance(current_dna, Mapping) else None
    )
    current_reference_integrity = current_candidate.get("source_reference_integrity")
    if (
        isinstance(current_reference_integrity, Mapping)
        and current_reference_integrity.get("valid") is False
    ):
        current_validation = {
            **current_validation,
            "valid": False,
            "reasons": [
                *current_validation.get("reasons", []),
                {
                    "rule": "HARD-INVALID-IMMUTABLE-REFERENCE",
                    "reason": "Current endpoint source fingerprint or decision-snapshot integrity failed.",
                    "location": "current_candidate.source_reference_integrity",
                },
            ],
        }
    result: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "calculation_version": COMPARISON_CALCULATION_VERSION,
        "role_class_version": ROLE_CLASS_VERSION,
        "compatibility_matrix_version": COMPATIBILITY_MATRIX_VERSION,
        "family_matrix_version": FAMILY_MATRIX_VERSION,
        "configuration": asdict(settings),
        "current_candidate": _candidate_identity(current_candidate),
        "current_endpoint_validation": current_validation,
        "eligible": [],
        "excluded": [],
        "policy": "Endpoint-DNA structural filtering only; no ranking or numerical matching is performed.",
        "ordering_policy": "Stable market_episode_id and experience_case_id order; this is not a rank.",
    }
    ordered = sorted(
        (deepcopy(dict(item)) for item in historical_cases),
        key=lambda item: (
            str(item.get("market_episode_id") or ""),
            str(item.get("experience_case_id") or ""),
        ),
    )
    if not current_validation["valid"]:
        for item in ordered:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="CURRENT-ENDPOINT-INVALID",
                    reason="The current candidate endpoint DNA failed mandatory validation.",
                    location="current_candidate.endpoint_dna",
                    missing_groups=current_validation["missing_groups"],
                )
            )
        result["status"] = "current_candidate_invalid"
        result["counts"] = {
            "historical_cases_examined": len(ordered),
            "eligible": 0,
            "excluded": len(result["excluded"]),
        }
        return result
    assert isinstance(current_dna, Mapping)
    current_descriptor = endpoint_descriptor(current_dna)
    result["current_candidate"]["structural_descriptor"] = current_descriptor
    current_source = current_descriptor["source"]
    mandatory_current = {
        "provider": current_source.get("provider"),
        "feed_identity": current_source.get("feed_identity"),
        "timeframe": current_source.get("timeframe"),
    }
    if any(not str(value or "").strip() or str(value).lower() == "unknown" for value in mandatory_current.values()):
        for item in ordered:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="CURRENT-MANDATORY-PROVENANCE-MISSING",
                    reason="Current endpoint DNA lacks mandatory provider, feed, or timeframe provenance.",
                    location="current_candidate.endpoint_dna.groups.source_identity",
                )
            )
        result["status"] = "current_candidate_invalid"
        result["counts"] = {
            "historical_cases_examined": len(ordered),
            "eligible": 0,
            "excluded": len(result["excluded"]),
        }
        return result

    for item in ordered:
        case_id = str(item.get("experience_case_id") or "")
        episode_id = str(item.get("market_episode_id") or "")
        if case_id and case_id == str(current_candidate.get("experience_case_id") or ""):
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-SELF-MATCH",
                    reason="A case cannot compare with itself.",
                    location="pre_matrix.self_match",
                )
            )
            continue
        if episode_id and episode_id == str(current_candidate.get("market_episode_id") or ""):
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-SAME-ACTIVE-EPISODE",
                    reason="Another version of the current market episode is excluded to prevent observation leakage.",
                    location="pre_matrix.market_episode_id",
                )
            )
            continue
        if item.get("active_version") is not True:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-INACTIVE-EPISODE-VERSION",
                    reason="Only the active version of each historical market episode is eligible.",
                    location="pre_matrix.active_version",
                )
            )
            continue
        state = str(item.get("state") or "")
        quality = str(item.get("quality_status") or "")
        if state == "quarantined" or quality == "quarantined":
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-QUARANTINED",
                    reason="Quarantined experience cases cannot enter the structural comparison pool.",
                    location="pre_matrix.review_state",
                )
            )
            continue
        if state != "accepted" or item.get("accepted_pool_eligible") is not True:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-UNREVIEWED",
                    reason="Only explicitly human-accepted experience cases are eligible.",
                    location="pre_matrix.review_state",
                )
            )
            continue
        if item.get("experience_schema_version", EXPERIENCE_SCHEMA_VERSION) != EXPERIENCE_SCHEMA_VERSION:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-UNSUPPORTED-EXPERIENCE-SCHEMA",
                    reason="Historical experience schema is unsupported.",
                    location="pre_matrix.experience_schema_version",
                )
            )
            continue
        source_reference_integrity = item.get("source_reference_integrity")
        if (
            isinstance(source_reference_integrity, Mapping)
            and source_reference_integrity.get("valid") is False
        ):
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-INVALID-IMMUTABLE-REFERENCE",
                    reason="Historical endpoint source fingerprint or decision-snapshot integrity failed.",
                    location="pre_matrix.source_reference_integrity",
                )
            )
            continue
        historical_dna = item.get("endpoint_dna")
        validation = validate_endpoint_dna(
            historical_dna if isinstance(historical_dna, Mapping) else None
        )
        if not validation["valid"]:
            first = validation["reasons"][0]
            result["excluded"].append(
                _excluded(
                    item,
                    rule=str(first["rule"]),
                    reason=str(first["reason"]),
                    location=str(first["location"]),
                    missing_groups=validation["missing_groups"],
                )
            )
            continue
        assert isinstance(historical_dna, Mapping)
        historical_descriptor = endpoint_descriptor(historical_dna)
        historical_source = historical_descriptor["source"]
        mandatory_historical = {
            "provider": historical_source.get("provider"),
            "feed_identity": historical_source.get("feed_identity"),
            "timeframe": historical_source.get("timeframe"),
        }
        if (
            any(
                not str(value or "").strip() or str(value).lower() == "unknown"
                for value in mandatory_historical.values()
            )
            or historical_source.get("mandatory_provenance_status") == "incompatible"
        ):
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-INCOMPATIBLE-MANDATORY-PROVENANCE",
                    reason="Historical endpoint lacks compatible mandatory provider, feed, or timeframe provenance.",
                    location="endpoint_dna.groups.source_identity",
                    missing_groups=validation["missing_groups"],
                )
            )
            continue
        current_role = current_descriptor["role"]
        historical_role = historical_descriptor["role"]
        if current_role.get("status") != "recognized" or historical_role.get(
            "status"
        ) != "recognized":
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-UNRESOLVED-ROLE-CLASS",
                    reason="Both endpoints require explicit family, position, and internal role classification.",
                    location="role_classification",
                    missing_groups=validation["missing_groups"],
                )
            )
            continue
        role_rule = role_compatibility(
            str(current_role["role_class"]), str(historical_role["role_class"])
        )
        family_rule = family_compatibility(
            str(current_descriptor["family_class"]),
            str(historical_descriptor["family_class"]),
        )
        combined = _combined_compatibility(role_rule, family_rule)
        compatibility_rule = {
            "role": role_rule,
            "family": family_rule,
            "matrix_version": COMPATIBILITY_MATRIX_VERSION,
        }
        matrix_location = (
            f"{role_rule.get('matrix_location')} + {family_rule.get('matrix_location')}"
        )
        if combined == "incompatible":
            incompatible_reason = (
                role_rule["reason"]
                if role_rule["compatibility_class"] == "incompatible"
                else family_rule["reason"]
            )
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-STRUCTURAL-INCOMPATIBILITY",
                    reason=incompatible_reason,
                    location=matrix_location,
                    missing_groups=validation["missing_groups"],
                    compatibility_class=combined,
                    compatibility_rule=compatibility_rule,
                )
            )
            continue
        if combined == "context_only" and not settings.allow_context_family:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="CONFIG-CONTEXT-FAMILY-DISABLED",
                    reason="The structural matrix permits context only, and context-family comparison is disabled.",
                    location=matrix_location,
                    missing_groups=validation["missing_groups"],
                    compatibility_class=combined,
                    compatibility_rule=compatibility_rule,
                )
            )
            continue
        degree = degree_relationship(
            current_descriptor.get("elliott_degree"),
            historical_descriptor.get("elliott_degree"),
        )
        if degree["relationship"] == "adjacent_degree" and (
            settings.same_degree_only or not settings.allow_adjacent_degree
        ):
            result["excluded"].append(
                _excluded(
                    item,
                    rule="CONFIG-ADJACENT-DEGREE-DISABLED",
                    reason="Adjacent-degree comparison is disabled by configuration.",
                    location="degree_matrix.adjacent_degree",
                    missing_groups=validation["missing_groups"],
                    compatibility_class=combined,
                    compatibility_rule={**compatibility_rule, "degree": degree},
                )
            )
            continue
        if degree["relationship"] == "far_degree" and not settings.allow_far_degree:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="HARD-FAR-DEGREE",
                    reason="Far-degree comparison requires explicit opt-in.",
                    location="degree_matrix.far_degree",
                    missing_groups=validation["missing_groups"],
                    compatibility_class=combined,
                    compatibility_rule={**compatibility_rule, "degree": degree},
                )
            )
            continue
        penalties, optional_missing, incomparable, cross_market = _soft_metadata(
            current_descriptor, historical_descriptor, degree
        )
        if cross_market and not settings.allow_cross_market:
            result["excluded"].append(
                _excluded(
                    item,
                    rule="CONFIG-CROSS-MARKET-DISABLED",
                    reason="Cross-market analogues require explicit opt-in.",
                    location="configuration.allow_cross_market",
                    missing_groups=[*validation["missing_groups"], *optional_missing],
                    incomparable_groups=incomparable,
                    compatibility_class=combined,
                    compatibility_rule={**compatibility_rule, "degree": degree},
                    soft_penalties=penalties,
                )
            )
            continue
        level = _comparison_level(
            role_class=str(role_rule["compatibility_class"]),
            family_class=str(family_rule["compatibility_class"]),
            degree_relation=str(degree["relationship"]),
            cross_market=cross_market,
        )
        reason = (
            f"Level {level}: {combined} structural roles; "
            f"{degree['relationship'].replace('_', ' ')}"
            + ("; cross-market normalized analogue." if cross_market else ".")
        )
        result["eligible"].append(
            {
                **_candidate_identity(item),
                "eligible": True,
                "excluded": False,
                "reason": reason,
                "comparison_reason": reason,
                "rule": "STRUCTURAL-FILTER-PASSED",
                "matrix_location": matrix_location,
                "comparison_level": level,
                "compatibility_class": combined,
                "compatibility_rule": {
                    **compatibility_rule,
                    "degree": degree,
                },
                "missing_groups": sorted(
                    set(validation["missing_groups"]).union(optional_missing)
                ),
                "incomparable_groups": incomparable,
                "soft_penalties": penalties,
                "comparison_scope": (
                    "normalized_endpoint_groups_only"
                    if cross_market
                    else "structural_endpoint_dna_only"
                ),
                "structural_descriptor": historical_descriptor,
            }
        )
    result["status"] = "completed"
    result["counts"] = {
        "historical_cases_examined": len(ordered),
        "eligible": len(result["eligible"]),
        "excluded": len(result["excluded"]),
    }
    return result


def compatibility_matrix_manifest() -> dict[str, Any]:
    """Expose the exhaustive matrices for audit without deriving any ranking."""
    counts = {
        compatibility: sum(
            1
            for row in ROLE_COMPATIBILITY_MATRIX.values()
            for rule in row.values()
            if rule["compatibility_class"] == compatibility
        )
        for compatibility in COMPATIBILITY_CLASSES
    }
    family_counts = {
        compatibility: sum(
            1
            for row in FAMILY_COMPATIBILITY_MATRIX.values()
            for rule in row.values()
            if rule["compatibility_class"] == compatibility
        )
        for compatibility in COMPATIBILITY_CLASSES
    }
    return {
        "role_class_version": ROLE_CLASS_VERSION,
        "compatibility_matrix_version": COMPATIBILITY_MATRIX_VERSION,
        "family_matrix_version": FAMILY_MATRIX_VERSION,
        "role_classes": deepcopy(ROLE_CLASS_DEFINITIONS),
        "role_matrix": deepcopy(ROLE_COMPATIBILITY_MATRIX),
        "role_matrix_counts": counts,
        "family_classes": list(FAMILY_CLASSES),
        "family_matrix": deepcopy(FAMILY_COMPATIBILITY_MATRIX),
        "family_matrix_counts": family_counts,
        "search_levels": {
            "1": "Exact role, exact family, same degree.",
            "2": "Exact role, exact family, adjacent degree.",
            "3": "Compatible or explicitly enabled context structure, or opted-in far degree.",
            "4": "Explicitly enabled cross-market analogue using normalized endpoint fields only.",
        },
        "policy": "The matrices classify eligibility only and contain no numerical score or rank.",
    }
