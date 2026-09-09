"""Correction-family semantics and non-probabilistic hypothesis handling."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping


MOTIVE_FAMILIES = frozenset({"impulse", "diagonal"})
CORRECTIVE_FAMILIES = frozenset({"zigzag", "flat", "triangle", "combination"})
STRUCTURAL_INVALIDATION_BASES = frozenset(
    {
        "hard_rule",
        "hard_structural_rule",
        "structural_rule",
        "structural_invalidation",
        "explicit_invalidation_level",
        "explicit_price_invalidation",
        "price_invalidation_level",
    }
)


def structure_family(value: Any) -> str:
    """Normalize a declared pattern name without inferring it from a wave label."""
    text = str(value or "").strip().lower().replace("_", "-")
    if "diagonal" in text:
        return "diagonal"
    if "impulse" in text or "motive" in text:
        return "impulse"
    if "zigzag" in text:
        return "zigzag"
    if "flat" in text:
        return "flat"
    if "triangle" in text:
        return "triangle"
    if any(
        token in text
        for token in (
            "combination",
            "double-three",
            "double three",
            "triple-three",
            "triple three",
            "w-x-y",
            "wxy",
        )
    ):
        return "combination"
    return "other"


def combination_variant(value: Any, positions: Iterable[str] = ()) -> str:
    """Return double or triple without changing any stored sequence position."""
    text = str(value or "").strip().lower().replace("_", "-")
    position_set = {str(position) for position in positions}
    if (
        "triple" in text
        or "x2" in text
        or "w-x-y-x-z" in text
        or "wxyxz" in text.replace("-", "")
    ):
        return "triple"
    if "double" in text:
        return "double"
    if {"X2", "Z"} & position_set:
        return "triple"
    return "double"


def map_sequence_children(
    parent: Mapping[str, Any],
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """Map child positions while preserving the first X and aliasing a legacy second X.

    Older triple-three payloads may contain two distinct child nodes whose
    sequence_position is ``X``. When no explicit ``X2`` is present, only the
    later legacy X receives the in-memory X2 alias. Stored JSON is never changed.
    """
    children = [
        node_by_id[str(child_id)]
        for child_id in parent.get("child_wave_ids", [])
        if str(child_id) in node_by_id
    ]
    explicit_x2 = any(
        str(child.get("sequence_position")) == "X2" for child in children
    )
    legacy_x_children = [
        child for child in children if str(child.get("sequence_position")) == "X"
    ]
    legacy_x2_id: str | None = None
    if not explicit_x2 and len(legacy_x_children) >= 2:
        dated_children = [
            child
            for child in legacy_x_children
            if isinstance(child.get("start"), Mapping)
            and isinstance(child["start"].get("date"), str)
        ]
        ordered_x_children = (
            sorted(dated_children, key=lambda child: child["start"]["date"])
            if len(dated_children) == len(legacy_x_children)
            else legacy_x_children
        )
        legacy_x2_id = str(ordered_x_children[-1].get("wave_id"))

    mapped: dict[str, Mapping[str, Any]] = {}
    duplicates: list[str] = []
    for child in children:
        position = str(child.get("sequence_position"))
        if position == "X" and str(child.get("wave_id")) == legacy_x2_id:
            position = "X2"
        if position in mapped:
            duplicates.append(position)
            continue
        mapped[position] = child
    return mapped, duplicates


def preserve_structural_hypotheses(
    hypotheses: Iterable[Mapping[str, Any]],
    invalidation_evidence: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Retain candidates unless hard structure or an explicit level invalidates them."""
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for raw in hypotheses:
        hypothesis = deepcopy(dict(raw))
        hypothesis_id = str(hypothesis.get("hypothesis_id") or "").strip()
        if not hypothesis_id or hypothesis_id in candidates_by_id:
            continue
        for field in (
            "supporting_evidence",
            "contradictory_evidence",
            "unavailable_or_incomparable_evidence",
            "evidence_needed_for_confirmation",
        ):
            value = hypothesis.get(field)
            hypothesis[field] = list(value) if isinstance(value, list) else []
        candidates_by_id[hypothesis_id] = hypothesis

    accepted_invalidations: dict[str, list[dict[str, str]]] = {}
    ignored_invalidations: list[dict[str, str]] = []
    invalidated_ids: set[str] = set()
    for raw in invalidation_evidence:
        item = dict(raw)
        hypothesis_id = str(item.get("hypothesis_id") or "").strip()
        basis = str(item.get("basis") or "").strip().lower()
        reason = str(item.get("reason") or "").strip()
        normalized = {
            "hypothesis_id": hypothesis_id,
            "basis": basis,
            "reason": reason,
        }
        if (
            hypothesis_id in candidates_by_id
            and basis in STRUCTURAL_INVALIDATION_BASES
            and reason
        ):
            invalidated_ids.add(hypothesis_id)
            accepted_invalidations.setdefault(hypothesis_id, []).append(normalized)
        else:
            ignored_invalidations.append(normalized)

    surviving = [
        hypothesis
        for hypothesis_id, hypothesis in candidates_by_id.items()
        if hypothesis_id not in invalidated_ids
    ]
    invalidated = [
        {
            **candidates_by_id[hypothesis_id],
            "status": "structurally_invalid",
            "structural_invalidations": evidence,
        }
        for hypothesis_id, evidence in accepted_invalidations.items()
    ]
    status = (
        "ambiguous"
        if len(surviving) > 1
        else "single_structural_candidate"
        if len(surviving) == 1
        else "no_structurally_valid_candidate"
    )
    candidate_status = "unresolved" if len(surviving) > 1 else "structurally_valid"
    for hypothesis in surviving:
        hypothesis["status"] = candidate_status

    return {
        "status": status,
        "candidate_hypotheses": surviving,
        "selected_hypothesis": surviving[0] if len(surviving) == 1 else None,
        "structural_invalidations": invalidated,
        "ignored_nonstructural_invalidations": ignored_invalidations,
        "policy": (
            "Indicators can support or contradict a candidate but cannot remove it. "
            "Only a hard structural rule or explicit invalidation level can do so."
        ),
    }
