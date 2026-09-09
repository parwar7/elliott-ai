"""Deterministic hierarchy checks and same-timeframe wave verification."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .correction_semantics import (
    CORRECTIVE_FAMILIES,
    MOTIVE_FAMILIES,
    combination_variant,
    map_sequence_children,
    structure_family,
)
from .market_data import add_ewo, infer_timeframe, load_candles, parse_date
from .schema import (
    HIERARCHY_METADATA_KINDS,
    STANDARD_DEGREES,
    VERIFICATION_REASON_CODES,
)
from .timeframes import (
    normalize_timeframe_name,
    timeframe_duration_seconds,
    timeframe_is_compatible_with_degree,
)


FINAL_DEGREE_CONFIDENCE = 0.75

def _structure_family(value: Any) -> str:
    return structure_family(value)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _anchor(node: dict[str, Any], side: str, field: str) -> Any:
    anchor = node.get(side)
    return anchor.get(field) if isinstance(anchor, dict) else None


def _finite_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and math.isfinite(number) else None


def _has_complete_anchor(node: Mapping[str, Any], side: str) -> bool:
    return (
        parse_date(_anchor(dict(node), side, "date")) is not None
        and _finite_number(_anchor(dict(node), side, "price")) is not None
    )


def _has_explicit_invalidation(node: Mapping[str, Any]) -> bool:
    return isinstance(node.get("invalidation"), str) and bool(
        node.get("invalidation", "").strip()
    )


def _timeframe_is_valid(node: Mapping[str, Any]) -> bool:
    degree = node.get("degree")
    timeframe = node.get("timeframe")
    return degree != "Unassigned" and timeframe_is_compatible_with_degree(
        degree, timeframe
    )


def _same_anchor(
    left: Mapping[str, Any], left_side: str, right: Mapping[str, Any], right_side: str
) -> bool:
    left_date = parse_date(_anchor(dict(left), left_side, "date"))
    right_date = parse_date(_anchor(dict(right), right_side, "date"))
    left_price = _finite_number(_anchor(dict(left), left_side, "price"))
    right_price = _finite_number(_anchor(dict(right), right_side, "price"))
    return (
        left_date is not None
        and right_date is not None
        and left_price is not None
        and right_price is not None
        and left_date == right_date
        and math.isclose(left_price, right_price, rel_tol=1e-9, abs_tol=1e-9)
    )


def _control_metadata_kind(node: Mapping[str, Any]) -> str | None:
    """Return a metadata kind only for an unmistakable non-wave control interval."""
    if (
        node.get("degree") != "Unassigned"
        or node.get("degree_status") != "unassigned"
        or node.get("parent_wave_id") is not None
        or node.get("sequence_position") != "0"
        or node.get("completion_status") != "unknown"
        or node.get("child_wave_ids") not in ([], None)
    ):
        return None
    description = f"{node.get('wave', '')} {node.get('structure', '')}".casefold()
    if "origin-control" in description or "origin control" in description:
        return "origin_control"
    if "gap-control" in description or "gap control" in description:
        return "gap_control"
    return None


def _control_metadata_from_node(node: Mapping[str, Any], kind: str) -> dict[str, Any]:
    return {
        "metadata_id": str(node.get("wave_id", "")),
        "kind": kind,
        "description": str(node.get("structure", "")).strip(),
        "start": deepcopy(node.get("start")),
        "end": deepcopy(node.get("end")),
        "evidence": list(node.get("evidence", [])),
        "concerns": list(node.get("concerns", [])),
    }


def _expected_child_positions(
    parent: Mapping[str, Any], positions: set[str]
) -> tuple[str, ...] | None:
    family = _structure_family(parent.get("structure"))
    if family in {"impulse", "diagonal"}:
        return ("1", "2", "3", "4", "5")
    if family in {"zigzag", "flat"}:
        return ("A", "B", "C")
    if family == "triangle":
        return ("A", "B", "C", "D", "E")
    if family == "combination":
        return (
            ("W", "X", "Y", "X2", "Z")
            if combination_variant(parent.get("structure"), positions) == "triple"
            else ("W", "X", "Y")
        )
    return None


def _children_match_family(
    family: str, children_by_position: Mapping[str, Mapping[str, Any]]
) -> bool:
    def family_at(position: str) -> str:
        return _structure_family(children_by_position[position].get("structure"))

    if family in {"impulse", "diagonal"}:
        return all(family_at(position) in MOTIVE_FAMILIES for position in ("1", "3", "5")) and all(
            family_at(position) in CORRECTIVE_FAMILIES for position in ("2", "4")
        )
    if family == "zigzag":
        return (
            family_at("A") in MOTIVE_FAMILIES
            and family_at("B") in CORRECTIVE_FAMILIES
            and family_at("C") in MOTIVE_FAMILIES
        )
    if family == "flat":
        return (
            family_at("A") in CORRECTIVE_FAMILIES
            and family_at("B") in CORRECTIVE_FAMILIES
            and family_at("C") in MOTIVE_FAMILIES
        )
    if family == "triangle":
        return all(
            family_at(position) in CORRECTIVE_FAMILIES
            for position in ("A", "B", "C", "D", "E")
        )
    if family == "combination":
        return all(
            family_at(position) in CORRECTIVE_FAMILIES
            for position in children_by_position
        )
    return False


def _motive_price_rules_hold(
    parent: Mapping[str, Any], children_by_position: Mapping[str, Mapping[str, Any]]
) -> bool:
    if not all(str(number) in children_by_position for number in range(1, 6)):
        return False
    waves = children_by_position
    p0 = _finite_number(_anchor(dict(waves["1"]), "start", "price"))
    p1 = _finite_number(_anchor(dict(waves["1"]), "end", "price"))
    p2 = _finite_number(_anchor(dict(waves["2"]), "end", "price"))
    p3 = _finite_number(_anchor(dict(waves["3"]), "end", "price"))
    p4 = _finite_number(_anchor(dict(waves["4"]), "end", "price"))
    p5 = _finite_number(_anchor(dict(waves["5"]), "end", "price"))
    if None in (p0, p1, p2, p3, p4, p5):
        return False
    direction = parent.get("direction")
    bullish = direction == "up" or (direction == "unknown" and p5 > p0)
    bearish = direction == "down" or (direction == "unknown" and p5 < p0)
    if not bullish and not bearish:
        return False
    family = _structure_family(parent.get("structure"))
    if bullish:
        if p2 < p0 or p3 <= p1 or (family == "impulse" and p4 <= p1):
            return False
    else:
        if p2 > p0 or p3 >= p1 or (family == "impulse" and p4 >= p1):
            return False
    lengths = {"1": abs(p1 - p0), "3": abs(p3 - p2), "5": abs(p5 - p4)}
    return not (lengths["3"] < lengths["1"] and lengths["3"] < lengths["5"])


def _child_is_structurally_verified(child: Mapping[str, Any]) -> bool:
    if (
        child.get("completion_status") != "completed"
        or child.get("degree_status") != "confirmed"
        or not _timeframe_is_valid(child)
        or not _has_complete_anchor(child, "start")
        or not _has_complete_anchor(child, "end")
        or not _has_explicit_invalidation(child)
        or child.get("verification_reason_codes")
    ):
        return False
    return child.get("child_structure_status") == "verified" or (
        child.get("child_structure_status") == "not_required"
        and child.get("terminal_at_available_resolution") is True
    )


def _node_depth(node: Mapping[str, Any], node_by_id: Mapping[str, Mapping[str, Any]]) -> int:
    depth = 0
    seen: set[str] = set()
    parent_id = node.get("parent_wave_id")
    while isinstance(parent_id, str) and parent_id in node_by_id and parent_id not in seen:
        seen.add(parent_id)
        depth += 1
        parent_id = node_by_id[parent_id].get("parent_wave_id")
    return depth


def _ordered_reason_codes(codes: set[str]) -> list[str]:
    return [code for code in VERIFICATION_REASON_CODES if code in codes]


def _parent_verification_reason_codes(
    parent: Mapping[str, Any],
    *,
    node_by_id: Mapping[str, Mapping[str, Any]],
    actual_children: Mapping[str, set[str]],
) -> list[str]:
    """Derive a parent verification state from declared nodes, never model wording."""
    reasons: set[str] = set()
    parent_id = str(parent.get("wave_id", ""))
    if parent.get("completion_status") != "completed":
        reasons.add("parent_not_completed")
    if parent.get("degree") == "Unassigned" or parent.get("degree_status") == "unassigned":
        reasons.add("parent_degree_unassigned")
    if not _has_complete_anchor(parent, "start") or not _has_complete_anchor(parent, "end"):
        reasons.add("parent_pivot_missing")
    if not _timeframe_is_valid(parent):
        reasons.add("parent_timeframe_invalid")
    if not _has_explicit_invalidation(parent):
        reasons.add("parent_invalidation_missing")

    declared_raw = parent.get("child_wave_ids")
    declared = (
        [item for item in declared_raw if isinstance(item, str)]
        if isinstance(declared_raw, list)
        else []
    )
    if parent.get("terminal_at_available_resolution") is True and declared:
        reasons.add("parent_terminal_with_children")
    if set(declared) != set(actual_children.get(parent_id, set())) or len(declared) != len(set(declared)):
        reasons.add("child_link_mismatch")

    children_by_position, duplicate_positions = map_sequence_children(parent, node_by_id)
    expected = _expected_child_positions(parent, set(children_by_position))
    family = _structure_family(parent.get("structure"))
    if expected is None:
        reasons.add("unsupported_parent_structure")
    elif (
        duplicate_positions
        or len(declared) != len(expected)
        or set(children_by_position) != set(expected)
    ):
        reasons.add("child_sequence_invalid")
    if (
        expected is not None
        and set(children_by_position) == set(expected)
        and not _children_match_family(family, children_by_position)
    ):
        reasons.add("child_sequence_invalid")

    degree_rank = {degree: index for index, degree in enumerate(STANDARD_DEGREES[:-1])}
    child_timeframes: set[str] = set()
    for child in children_by_position.values():
        if child.get("completion_status") != "completed":
            reasons.add("child_not_completed")
        if child.get("degree_status") != "confirmed":
            reasons.add("child_degree_not_confirmed")
        if not _has_complete_anchor(child, "start") or not _has_complete_anchor(child, "end"):
            reasons.add("child_pivot_missing")
        if not _timeframe_is_valid(child):
            reasons.add("child_timeframe_invalid")
        else:
            child_timeframes.add(str(child.get("timeframe")))
        if not _has_explicit_invalidation(child):
            reasons.add("child_invalidation_missing")
        if not _child_is_structurally_verified(child):
            reasons.add("child_not_structurally_verified")
        parent_degree = parent.get("degree")
        child_degree = child.get("degree")
        if (
            parent_degree not in degree_rank
            or child_degree not in degree_rank
            or degree_rank[child_degree] <= degree_rank[parent_degree]
        ):
            reasons.add("hierarchy_degree_invalid")
        parent_start = parse_date(_anchor(dict(parent), "start", "date"))
        parent_end = parse_date(_anchor(dict(parent), "end", "date"))
        child_start = parse_date(_anchor(dict(child), "start", "date"))
        child_end = parse_date(_anchor(dict(child), "end", "date"))
        if (
            parent_start is not None
            and child_start is not None
            and child_start < parent_start
        ) or (
            parent_end is not None and child_end is not None and child_end > parent_end
        ):
            reasons.add("child_outside_parent")
    if len(child_timeframes) != 1:
        reasons.add("child_timeframe_inconsistent")
    parent_timeframe = str(parent.get("timeframe"))
    try:
        parent_duration = timeframe_duration_seconds(parent_timeframe)
        if any(
            timeframe_duration_seconds(normalize_timeframe_name(timeframe))
            > parent_duration
            for timeframe in child_timeframes
        ):
            reasons.add("child_timeframe_inconsistent")
    except ValueError:
        reasons.add("child_timeframe_inconsistent")

    if expected is not None and set(children_by_position) == set(expected):
        first = children_by_position[expected[0]]
        last = children_by_position[expected[-1]]
        if not _same_anchor(parent, "start", first, "start") or not _same_anchor(
            parent, "end", last, "end"
        ):
            reasons.add("parent_child_boundary_mismatch")
        for previous, current in zip(expected, expected[1:]):
            if not _same_anchor(
                children_by_position[previous], "end", children_by_position[current], "start"
            ):
                reasons.add("child_pivot_chain_discontinuous")
        if family in {"impulse", "diagonal"} and not _motive_price_rules_hold(
            parent, children_by_position
        ):
            reasons.add("hard_price_rule_failure")
    return _ordered_reason_codes(reasons)


def recompute_degree_hierarchy_verification(response: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic hierarchy view without trusting model verified claims.

    A terminal node may remain ``not_required`` at the supplied resolution limit.
    Every non-terminal parent is recomputed bottom-up and may become ``verified``
    only from its explicit child graph, anchors, timeframe matrix, and hard rules.
    """
    normalized = deepcopy(dict(response))
    raw_nodes = normalized.get("degree_hierarchy")
    if not isinstance(raw_nodes, list):
        return normalized

    existing_metadata = normalized.get("hierarchy_metadata")
    metadata = [dict(item) for item in existing_metadata if isinstance(item, dict)] if isinstance(existing_metadata, list) else []
    nodes: list[dict[str, Any]] = []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        node = dict(raw_node)
        kind = _control_metadata_kind(node)
        if kind in HIERARCHY_METADATA_KINDS:
            metadata.append(_control_metadata_from_node(node, kind))
        else:
            nodes.append(node)
    normalized["degree_hierarchy"] = nodes
    if metadata or "hierarchy_metadata" in normalized:
        unique_metadata: dict[str, dict[str, Any]] = {}
        for item in metadata:
            metadata_id = item.get("metadata_id")
            if isinstance(metadata_id, str) and metadata_id not in unique_metadata:
                unique_metadata[metadata_id] = item
        normalized["hierarchy_metadata"] = list(unique_metadata.values())

    node_by_id = {
        str(node["wave_id"]): node
        for node in nodes
        if isinstance(node.get("wave_id"), str) and node.get("wave_id")
    }
    actual_children: dict[str, set[str]] = {}
    for node in nodes:
        wave_id = node.get("wave_id")
        parent_id = node.get("parent_wave_id")
        if isinstance(wave_id, str) and isinstance(parent_id, str):
            actual_children.setdefault(parent_id, set()).add(wave_id)

    ordered_nodes = sorted(
        node_by_id.values(),
        key=lambda node: (-_node_depth(node, node_by_id), str(node.get("wave_id"))),
    )
    for node in ordered_nodes:
        if (
            node.get("child_structure_status") == "not_required"
            and node.get("terminal_at_available_resolution") is True
        ):
            node["verification_reason_codes"] = []
            continue
        reasons = _parent_verification_reason_codes(
            node,
            node_by_id=node_by_id,
            actual_children=actual_children,
        )
        node["verification_reason_codes"] = reasons
        node["child_structure_status"] = "verified" if not reasons else "unproven"
    return normalized


def validate_degree_hierarchy_rules(response: dict[str, Any]) -> list[str]:
    """Apply parent containment, child-pattern, and classical impulse price rules."""
    nodes = [item for item in response.get("degree_hierarchy", []) if isinstance(item, dict)]
    node_by_id = {
        item["wave_id"]: item
        for item in nodes
        if isinstance(item.get("wave_id"), str) and item.get("wave_id")
    }
    errors: list[str] = []

    for child in nodes:
        parent = node_by_id.get(child.get("parent_wave_id"))
        if parent is None:
            continue
        parent_start = parse_date(_anchor(parent, "start", "date"))
        parent_end = parse_date(_anchor(parent, "end", "date"))
        child_start = parse_date(_anchor(child, "start", "date"))
        child_end = parse_date(_anchor(child, "end", "date"))
        if parent_start and child_start and child_start < parent_start:
            errors.append(
                f"Child {child.get('wave_id')!r} starts before parent {parent.get('wave_id')!r}."
            )
        if parent_end and child_end and child_end > parent_end:
            errors.append(
                f"Child {child.get('wave_id')!r} ends after parent {parent.get('wave_id')!r}."
            )

    for parent in nodes:
        if parent.get("child_structure_status") != "verified":
            continue
        children_by_position, duplicate_positions = map_sequence_children(
            parent, node_by_id
        )
        positions = set(children_by_position)
        family = _structure_family(parent.get("structure"))
        expected: set[str] | None = None
        if family in {"impulse", "diagonal"}:
            expected = {"1", "2", "3", "4", "5"}
        elif family in {"zigzag", "flat"}:
            expected = {"A", "B", "C"}
        elif family == "triangle":
            expected = {"A", "B", "C", "D", "E"}
        elif family == "combination":
            variant = combination_variant(parent.get("structure"), positions)
            expected = (
                {"W", "X", "Y", "X2", "Z"}
                if variant == "triple"
                else {"W", "X", "Y"}
            )
        if duplicate_positions:
            errors.append(
                f"Wave {parent.get('wave_id')!r} has duplicate child positions: "
                + ", ".join(sorted(set(duplicate_positions)))
                + "."
            )
        if expected is not None and positions != expected:
            missing = ", ".join(sorted(expected - positions)) or "none"
            extra = ", ".join(sorted(positions - expected)) or "none"
            errors.append(
                f"Wave {parent.get('wave_id')!r} marks {family} children verified with "
                f"missing positions [{missing}] and extra positions [{extra}]."
            )

        def require_family(
            position: str,
            allowed: frozenset[str],
            expectation: str,
        ) -> None:
            child = children_by_position.get(position)
            if child is None:
                return
            actual = _structure_family(child.get("structure"))
            if actual not in allowed:
                errors.append(
                    f"Wave {parent.get('wave_id')!r} {family} requires {expectation} "
                    f"at {position}; child {child.get('wave_id')!r} declares "
                    f"{child.get('structure')!r} ({actual})."
                )

        if family == "zigzag":
            require_family("A", MOTIVE_FAMILIES, "a motive structure or allowed diagonal")
            require_family("B", CORRECTIVE_FAMILIES, "a corrective structure")
            require_family("C", MOTIVE_FAMILIES, "a motive structure or allowed diagonal")
        elif family == "flat":
            require_family("A", CORRECTIVE_FAMILIES, "a corrective structure")
            require_family("B", CORRECTIVE_FAMILIES, "a corrective structure")
            require_family("C", MOTIVE_FAMILIES, "a motive structure or allowed diagonal")
        elif family == "triangle":
            for position in ("A", "B", "C", "D", "E"):
                require_family(position, CORRECTIVE_FAMILIES, "a corrective structure")
        elif family == "combination":
            for position in expected or ():
                require_family(position, CORRECTIVE_FAMILIES, "a valid corrective family")

        if family not in {"impulse", "diagonal"} or not {"1", "2", "3", "4", "5"}.issubset(positions):
            continue
        waves = children_by_position
        p0 = _number(_anchor(waves["1"], "start", "price"))
        p1 = _number(_anchor(waves["1"], "end", "price"))
        p2 = _number(_anchor(waves["2"], "end", "price"))
        p3 = _number(_anchor(waves["3"], "end", "price"))
        p4 = _number(_anchor(waves["4"], "end", "price"))
        p5 = _number(_anchor(waves["5"], "end", "price"))
        if None in (p0, p1, p2, p3, p4, p5):
            errors.append(
                f"Wave {parent.get('wave_id')!r} lacks prices required for impulse rule verification."
            )
            continue
        bullish = parent.get("direction") == "up" or (parent.get("direction") == "unknown" and p5 > p0)
        bearish = parent.get("direction") == "down" or (parent.get("direction") == "unknown" and p5 < p0)
        if not bullish and not bearish:
            errors.append(f"Wave {parent.get('wave_id')!r} has no usable motive direction.")
            continue
        if bullish:
            if p2 < p0:
                errors.append(f"Wave {parent.get('wave_id')!r}: Wave 2 moved below the Wave 1 origin.")
            if p3 <= p1:
                errors.append(f"Wave {parent.get('wave_id')!r}: Wave 3 did not exceed Wave 1.")
            if family == "impulse" and p4 <= p1:
                errors.append(f"Wave {parent.get('wave_id')!r}: Wave 4 overlaps Wave 1 territory.")
        else:
            if p2 > p0:
                errors.append(f"Wave {parent.get('wave_id')!r}: Wave 2 moved above the Wave 1 origin.")
            if p3 >= p1:
                errors.append(f"Wave {parent.get('wave_id')!r}: Wave 3 did not exceed Wave 1 downward.")
            if family == "impulse" and p4 >= p1:
                errors.append(f"Wave {parent.get('wave_id')!r}: Wave 4 overlaps Wave 1 territory.")
        lengths = {
            "1": abs(p1 - p0),
            "3": abs(p3 - p2),
            "5": abs(p5 - p4),
        }
        if lengths["3"] < lengths["1"] and lengths["3"] < lengths["5"]:
            errors.append(f"Wave {parent.get('wave_id')!r}: Wave 3 is the shortest actionary wave.")
    return errors


def _display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return path.name


def verify_resolved_impulses(
    response: dict[str, Any],
    market_paths: Iterable[Path],
    *,
    workspace: Path,
) -> list[dict[str, Any]]:
    """Aggregate EWO/volume for every resolved completed five-wave parent."""
    from scripts.elliott_analysis_layers import add_impulse_verification

    response = recompute_degree_hierarchy_verification(response)
    nodes = [item for item in response.get("degree_hierarchy", []) if isinstance(item, dict)]
    node_by_id = {
        item["wave_id"]: item
        for item in nodes
        if isinstance(item.get("wave_id"), str) and item.get("wave_id")
    }
    paths_by_timeframe: dict[str, Path] = {}
    for raw_path in market_paths:
        path = Path(raw_path).resolve()
        timeframe = infer_timeframe(path)
        if timeframe not in paths_by_timeframe:
            paths_by_timeframe[timeframe] = path

    candle_cache: dict[Path, list[dict[str, Any]]] = {}
    results: list[dict[str, Any]] = []
    for parent in nodes:
        if parent.get("completion_status") != "completed":
            continue
        if parent.get("child_structure_status") != "verified":
            continue
        if _structure_family(parent.get("structure")) not in {"impulse", "diagonal"}:
            continue
        children = [
            node_by_id[child_id]
            for child_id in parent.get("child_wave_ids", [])
            if child_id in node_by_id
        ]
        by_position = {str(child.get("sequence_position")): child for child in children}
        result: dict[str, Any] = {
            "evidence_id": f"V{len(results) + 1}",
            "parent_wave_id": parent.get("wave_id"),
            "parent_degree": parent.get("degree"),
            "timeframe": None,
            "source": None,
            "status": "Insufficient Data",
            "notes": "",
            "waves": [],
        }
        if not all(str(number) in by_position for number in range(1, 6)):
            result["notes"] = "A complete 1-2-3-4-5 child sequence was not supplied."
            results.append(result)
            continue
        timeframes = {str(by_position[str(number)].get("timeframe")) for number in range(1, 6)}
        if len(timeframes) != 1:
            result["notes"] = "Child waves do not use one unchanged timeframe."
            results.append(result)
            continue
        timeframe = next(iter(timeframes))
        path = paths_by_timeframe.get(timeframe)
        result["timeframe"] = timeframe
        if path is None:
            result["notes"] = f"No {timeframe} candle file covers this verified child sequence."
            results.append(result)
            continue
        result["source"] = _display_path(workspace, path)
        if path not in candle_cache:
            candle_cache[path] = add_ewo(load_candles(path))
        candles = candle_cache[path]
        rows: list[dict[str, Any]] = []
        for number in range(1, 6):
            child = by_position[str(number)]
            start = parse_date(_anchor(child, "start", "date"))
            end = parse_date(_anchor(child, "end", "date"))
            subset = [] if start is None or end is None else [
                candle
                for candle in candles
                if candle.get("parsed_date") is not None
                and start <= candle["parsed_date"] <= end
            ]
            ewo_values = [float(candle["ewo"]) for candle in subset if candle.get("ewo") is not None]
            volumes = [float(candle["volume"]) for candle in subset if candle.get("volume") is not None]
            ewo_min = min(ewo_values) if ewo_values else None
            ewo_max = max(ewo_values) if ewo_values else None
            row = {
                "sequence_id": parent.get("wave_id"),
                "wave_label": str(number),
                "wave_status": "completed",
                "start_date": _anchor(child, "start", "date"),
                "end_date": _anchor(child, "end", "date"),
                "start_price": _anchor(child, "start", "price"),
                "end_price": _anchor(child, "end", "price"),
                "cumulative_volume": sum(volumes) if volumes else None,
                "wave_ewo_min": ewo_min,
                "wave_ewo_max": ewo_max,
                "wave_ewo_peak": (
                    max(abs(ewo_min), abs(ewo_max))
                    if ewo_min is not None and ewo_max is not None
                    else None
                ),
                "ewo_zero_touch": (
                    ewo_min <= 0 <= ewo_max
                    if ewo_min is not None and ewo_max is not None
                    else None
                ),
                "bar_count": len(subset),
            }
            rows.append(row)
        verified, _ = add_impulse_verification(rows)
        if verified:
            result["status"] = verified[0].get("impulse_status", "Insufficient Data")
            result["notes"] = verified[0].get("impulse_notes", "")
        result["waves"] = [
            {
                "wave": row["wave_label"],
                "bar_count": row["bar_count"],
                "cumulative_volume": row["cumulative_volume"],
                "wave_ewo_peak": row["wave_ewo_peak"],
                "ewo_zero_touch": row["ewo_zero_touch"],
            }
            for row in rows
        ]
        results.append(result)
    return results


def assess_degree_readiness(
    response: dict[str, Any],
    *,
    validation_errors: list[str],
    source_run: dict[str, Any],
    impulse_verification: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive final-report readiness from deterministic checks only."""
    response = recompute_degree_hierarchy_verification(response)
    blockers: list[str] = []
    warnings: list[str] = []
    if validation_errors:
        blockers.append("Degree hierarchy has deterministic validation errors.")

    source_errors = source_run.get("validation", [])
    if source_errors:
        blockers.append("The source analysis run has validation errors.")
    reconciliation = source_run.get("evidence", {}).get("data_reconciliation", {}).get(
        "overall_status", "not_available"
    )
    if reconciliation not in {"passed", "passed_with_volume_limitations"}:
        blockers.append(f"Cross-timeframe reconciliation is {reconciliation}.")
    elif reconciliation == "passed_with_volume_limitations":
        warnings.append(
            "Cross-timeframe volume is quarantined; verification uses one unchanged timeframe per sequence."
        )

    nodes = [item for item in response.get("degree_hierarchy", []) if isinstance(item, dict)]
    if not nodes:
        blockers.append("No degree hierarchy was produced.")
    completed = [item for item in nodes if item.get("completion_status") == "completed"]
    if any(item.get("degree") == "Unassigned" for item in nodes):
        blockers.append("At least one wave still has an Unassigned degree.")
    if any(item.get("degree_status") != "confirmed" for item in completed):
        blockers.append("At least one completed wave still has a candidate or unassigned degree.")
    if any(
        item.get("child_structure_status") == "unproven"
        and not item.get("terminal_at_available_resolution")
        for item in completed
    ):
        blockers.append("At least one completed parent still has unproven internal structure.")

    required_impulses = [
        item
        for item in completed
        if item.get("child_structure_status") == "verified"
        and _structure_family(item.get("structure")) in {"impulse", "diagonal"}
    ]
    verification_by_parent = {
        item.get("parent_wave_id"): item for item in impulse_verification
    }
    missing_verification = [
        item.get("wave_id")
        for item in required_impulses
        if verification_by_parent.get(item.get("wave_id"), {}).get("status")
        in {None, "Insufficient Data"}
    ]
    if required_impulses and missing_verification:
        blockers.append(
            "Same-timeframe volume/EWO verification is incomplete for: "
            + ", ".join(str(item) for item in missing_verification)
            + "."
        )
    if not required_impulses:
        blockers.append("No completed five-wave parent has verified children for indicator aggregation.")

    return {
        "final_report_ready": not blockers,
        # Retained for serialized readiness compatibility; it is no longer a gate.
        "minimum_degree_confidence": FINAL_DEGREE_CONFIDENCE,
        "model_assertions_used_for_readiness": False,
        "hierarchy_valid": not validation_errors,
        "reconciliation_status": reconciliation,
        "completed_wave_count": len(completed),
        "impulse_sequences_checked": len(impulse_verification),
        "blockers": blockers,
        "warnings": warnings,
        "deterministic_impulse_verification": impulse_verification,
    }


def degree_sort_key(node: dict[str, Any]) -> tuple[int, str, str]:
    rank = {degree: index for index, degree in enumerate(STANDARD_DEGREES)}
    start = str(_anchor(node, "start", "date") or "")
    return rank.get(str(node.get("degree")), len(rank)), start, str(node.get("wave_id", ""))
