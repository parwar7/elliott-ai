"""Deterministic Fibonacci, duration, channel, alternation, and wave-feature metrics."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from .correction_semantics import map_sequence_children, structure_family
from .fingerprints import generate_response_fingerprints
from .indicators import (
    compare_rsi_pivots,
    enrich_candles,
    indicator_evidence,
    normalize_features,
    summarize_rsi_window,
)
from .market_data import (
    build_indicator_source_metadata,
    infer_timeframe,
    load_candles,
    load_market_metadata,
    parse_date,
)


IMPULSE_FIBONACCI = (0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.382, 1.618, 2.0, 2.618, 4.236)
CORRECTION_FIBONACCI = (0.236, 0.382, 0.5, 0.618, 0.786, 0.9, 1.0, 1.236, 1.382, 1.618)
RETRACEMENT_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)
PROJECTION_LEVELS = (0.618, 1.0, 1.272, 1.618, 2.0, 2.618)
HIGH_DEGREE_RATIO_SCALE = frozenset(
    ("Grand Supercycle", "Supercycle", "Cycle", "Primary")
)

HOUR = 1.0
DAY = 24.0 * HOUR
WEEK = 7.0 * DAY
MONTH = 30.4375 * DAY
YEAR = 365.25 * DAY


def _prior(lower: float | None, upper: float | None, unit_hours: float, label: str) -> dict[str, Any]:
    return {
        "minimum_hours": lower * unit_hours if lower is not None else None,
        "maximum_hours": upper * unit_hours if upper is not None else None,
        "label": label,
    }


# User-supplied degree-duration ranges are retained as soft diagnostics only.
DEGREE_DURATION_PRIORS: dict[str, dict[str, dict[str, Any]]] = {
    "Grand Supercycle": {
        "total": _prior(100, 300, YEAR, "100 to 300+ years"),
        "1": _prior(20, 50, YEAR, "20 to 50+ years"),
        "2": _prior(None, None, YEAR, "multi-decade"),
        "3": _prior(None, None, YEAR, "multi-decade to century+"),
        "4": _prior(None, None, YEAR, "multi-decade"),
        "5": _prior(None, None, YEAR, "multi-decade"),
        "A": _prior(None, None, YEAR, "multi-decade"),
        "B": _prior(None, None, YEAR, "multi-decade"),
        "C": _prior(None, None, YEAR, "multi-decade"),
    },
    "Supercycle": {
        "total": _prior(40, 70, YEAR, "40 to 70 years"),
        "1": _prior(8, 15, YEAR, "8 to 15 years"),
        "2": _prior(3, 6, YEAR, "3 to 6 years"),
        "3": _prior(12, 22, YEAR, "12 to 22 years"),
        "4": _prior(8, 15, YEAR, "8 to 15 years"),
        "5": _prior(5, 10, YEAR, "5 to 10 years"),
        "A": _prior(2, 4, YEAR, "2 to 4 years"),
        "B": _prior(4, 8, YEAR, "4 to 8 years"),
        "C": _prior(3, 6, YEAR, "3 to 6 years"),
    },
    "Cycle": {
        "total": _prior(1, 10, YEAR, "1 to 10 years, typically 3 to 7"),
        "1": _prior(6, 12, MONTH, "6 to 12 months"),
        "2": _prior(3, 6, MONTH, "3 to 6 months"),
        "3": _prior(12, 18, MONTH, "12 to 18 months"),
        "4": _prior(6, 14, MONTH, "6 to 14 months"),
        "5": _prior(4, 8, MONTH, "4 to 8 months"),
        "A": _prior(2, 5, MONTH, "2 to 5 months"),
        "B": _prior(5, 12, MONTH, "5 to 12 months"),
        "C": _prior(3, 8, MONTH, "3 to 8 months"),
    },
    "Primary": {
        "total": _prior(3, 24, MONTH, "several months to 2 years"),
        "1": _prior(1.5, 4, MONTH, "1.5 to 4 months"),
        "2": _prior(3, 8.7, WEEK, "3 weeks to 2 months"),
        "3": _prior(3, 6, MONTH, "3 to 6 months"),
        "4": _prior(2, 5, MONTH, "2 to 5 months"),
        "5": _prior(1, 3, MONTH, "1 to 3 months"),
        "A": _prior(3, 8.7, WEEK, "3 weeks to 2 months"),
        "B": _prior(1.5, 4, MONTH, "1.5 to 4 months"),
        "C": _prior(1, 3, MONTH, "1 to 3 months"),
    },
    "Intermediate": {
        "total": _prior(1, 6, MONTH, "1 to 6 months"),
        "1": _prior(1, 3, WEEK, "1 to 3 weeks"),
        "2": _prior(3, 10.5, DAY, "3 days to 1.5 weeks"),
        "3": _prior(2, 5, WEEK, "2 to 5 weeks"),
        "4": _prior(1.5, 4, WEEK, "1.5 to 4 weeks"),
        "5": _prior(1, 3, WEEK, "1 to 3 weeks"),
        "A": _prior(4, 10.5, DAY, "4 days to 1.5 weeks"),
        "B": _prior(1.5, 3, WEEK, "1.5 to 3 weeks"),
        "C": _prior(1, 2.5, WEEK, "1 to 2.5 weeks"),
    },
    "Minor": {
        "total": _prior(2, 6, WEEK, "2 to 6 weeks"),
        "1": _prior(2, 5, DAY, "2 to 5 days"),
        "2": _prior(1, 3, DAY, "1 to 3 days"),
        "3": _prior(4, 8, DAY, "4 to 8 days"),
        "4": _prior(3, 6, DAY, "3 to 6 days"),
        "5": _prior(2, 4, DAY, "2 to 4 days"),
        "A": _prior(1, 3, DAY, "1 to 3 days"),
        "B": _prior(2, 6, DAY, "2 to 6 days"),
        "C": _prior(2, 5, DAY, "2 to 5 days"),
    },
    "Minute": {
        "total": _prior(3, 10, DAY, "3 to 10 days"),
        "1": _prior(8, 24, HOUR, "8 to 24 hours"),
        "2": _prior(4, 12, HOUR, "4 to 12 hours"),
        "3": _prior(16, 40, HOUR, "16 to 40 hours"),
        "4": _prior(12, 36, HOUR, "12 to 36 hours"),
        "5": _prior(8, 20, HOUR, "8 to 20 hours"),
        "A": _prior(5, 15, HOUR, "5 to 15 hours"),
        "B": _prior(10, 30, HOUR, "10 to 30 hours"),
        "C": _prior(8, 25, HOUR, "8 to 25 hours"),
    },
    "Minuette": {
        "total": _prior(6.5, 13, HOUR, "1 to 2 trading sessions"),
        "1": _prior(1, 2.5, HOUR, "1 to 2.5 hours"),
        "2": _prior(0.5, 1.5, HOUR, "30 to 90 minutes"),
        "3": _prior(2, 4, HOUR, "2 to 4 hours"),
        "4": _prior(1.5, 3.5, HOUR, "1.5 to 3.5 hours"),
        "5": _prior(1, 2, HOUR, "1 to 2 hours"),
        "A": _prior(0.75, 1.5, HOUR, "45 to 90 minutes"),
        "B": _prior(1.5, 3, HOUR, "1.5 to 3 hours"),
        "C": _prior(1, 2.5, HOUR, "1 to 2.5 hours"),
    },
    "Subminuette": {
        "total": _prior(0.25, 4, HOUR, "minutes to a few hours"),
        "1": _prior(0.25, 35 / 60, HOUR, "15 to 35 minutes"),
        "2": _prior(5 / 60, 20 / 60, HOUR, "5 to 20 minutes"),
        "3": _prior(25 / 60, 1, HOUR, "25 to 60 minutes"),
        "4": _prior(20 / 60, 50 / 60, HOUR, "20 to 50 minutes"),
        "5": _prior(10 / 60, 0.5, HOUR, "10 to 30 minutes"),
        "A": _prior(10 / 60, 25 / 60, HOUR, "10 to 25 minutes"),
        "B": _prior(0.25, 40 / 60, HOUR, "15 to 40 minutes"),
        "C": _prior(10 / 60, 0.5, HOUR, "10 to 30 minutes"),
    },
}


def _family(value: Any) -> str:
    return structure_family(value)


def _anchor(node: dict[str, Any], side: str, field: str) -> Any:
    value = node.get(side)
    return value.get(field) if isinstance(value, dict) else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return abs(float(numerator)) / abs(float(denominator))


def _duration_days(node: dict[str, Any]) -> float | None:
    start = parse_date(_anchor(node, "start", "date"))
    end = parse_date(_anchor(node, "end", "date"))
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds() / 86_400.0


def _price_length(node: dict[str, Any], *, logarithmic: bool = False) -> float | None:
    start = _number(_anchor(node, "start", "price"))
    end = _number(_anchor(node, "end", "price"))
    if start is None or end is None or (logarithmic and (start <= 0 or end <= 0)):
        return None
    return abs(math.log(end / start)) if logarithmic else abs(end - start)


def _ratio_scale(parent: dict[str, Any]) -> str:
    duration = _duration_days(parent)
    start = _number(_anchor(parent, "start", "price"))
    end = _number(_anchor(parent, "end", "price"))
    move_pct = (
        abs(end / start - 1.0) * 100.0
        if start not in (None, 0) and end is not None and start > 0 and end > 0
        else None
    )
    if (
        parent.get("degree") in HIGH_DEGREE_RATIO_SCALE
        or (duration is not None and duration > 730)
        or (move_pct is not None and move_pct > 100)
    ):
        return "logarithmic"
    return "arithmetic"


def _duration_prior_result(
    *, actual_hours: float | None, prior: dict[str, Any] | None
) -> dict[str, Any]:
    if prior is None:
        return {"status": "not_available"}
    result = {"actual_hours": actual_hours, **prior}
    lower = prior.get("minimum_hours")
    upper = prior.get("maximum_hours")
    if actual_hours is None:
        result["status"] = "insufficient_data"
    elif lower is None or upper is None:
        result["status"] = "qualitative_only"
    elif actual_hours < float(lower):
        result["status"] = "shorter_than_soft_range"
    elif actual_hours > float(upper):
        result["status"] = "longer_than_soft_range"
    else:
        result["status"] = "within_soft_range"
    return result


def _nearest_fibonacci(value: float | None, levels: tuple[float, ...]) -> dict[str, Any] | None:
    if value is None:
        return None
    nearest = min(levels, key=lambda level: abs(value - level))
    return {
        "actual": value,
        "nearest_level": nearest,
        "absolute_difference": abs(value - nearest),
        "relative_error_pct": abs(value - nearest) / nearest * 100.0 if nearest else None,
    }


def _node_maps(response: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = [item for item in response.get("degree_hierarchy", []) if isinstance(item, dict)]
    return nodes, {
        str(item["wave_id"]): item
        for item in nodes
        if isinstance(item.get("wave_id"), str)
    }


def _children(parent: dict[str, Any], node_by_id: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped, _ = map_sequence_children(parent, node_by_id)
    return {position: dict(node) for position, node in mapped.items()}


def calculate_fibonacci_and_duration(response: dict[str, Any]) -> list[dict[str, Any]]:
    nodes, node_by_id = _node_maps(response)
    results: list[dict[str, Any]] = []
    for parent in nodes:
        children = _children(parent, node_by_id)
        family = _family(parent.get("structure"))
        selected_scale = _ratio_scale(parent)
        item: dict[str, Any] | None = None
        if family in {"impulse", "diagonal"} and all(str(number) in children for number in range(1, 6)):
            waves = {str(number): children[str(number)] for number in range(1, 6)}
            arithmetic_lengths = {
                number: _price_length(waves[number]) for number in waves
            }
            logarithmic_lengths = {
                number: _price_length(waves[number], logarithmic=True)
                for number in waves
            }

            def impulse_ratios(lengths: dict[str, float | None]) -> dict[str, float | None]:
                return {
                    "wave_2_retracement_of_wave_1": _ratio(lengths["2"], lengths["1"]),
                    "wave_3_extension_of_wave_1": _ratio(lengths["3"], lengths["1"]),
                    "wave_4_retracement_of_wave_3": _ratio(lengths["4"], lengths["3"]),
                    "wave_5_length_vs_wave_1": _ratio(lengths["5"], lengths["1"]),
                    "wave_5_length_vs_wave_3": _ratio(lengths["5"], lengths["3"]),
                }

            arithmetic_ratios = impulse_ratios(arithmetic_lengths)
            logarithmic_ratios = impulse_ratios(logarithmic_lengths)
            ratios = (
                logarithmic_ratios
                if selected_scale == "logarithmic"
                else arithmetic_ratios
            )
            durations = {number: _duration_days(waves[number]) for number in waves}
            item = {
                "parent_wave_id": parent.get("wave_id"),
                "family": family,
                "ratio_scale": selected_scale,
                "ratio_scale_rule": (
                    "Logarithmic for Primary and higher degree, spans over two years, "
                    "or moves over 100%; arithmetic otherwise. Both are retained."
                ),
                "price_lengths": arithmetic_lengths,
                "log_price_lengths": logarithmic_lengths,
                "duration_days": durations,
                "ratios": ratios,
                "arithmetic_ratios": arithmetic_ratios,
                "logarithmic_ratios": logarithmic_ratios,
                "fibonacci_matches": {
                    key: _nearest_fibonacci(value, IMPULSE_FIBONACCI)
                    for key, value in ratios.items()
                },
                "alternation": {
                    "wave_2_structure": waves["2"].get("structure"),
                    "wave_4_structure": waves["4"].get("structure"),
                    "retracement_difference": (
                        abs(
                            float(ratios["wave_2_retracement_of_wave_1"])
                            - float(ratios["wave_4_retracement_of_wave_3"])
                        )
                        if ratios["wave_2_retracement_of_wave_1"] is not None
                        and ratios["wave_4_retracement_of_wave_3"] is not None
                        else None
                    ),
                    "duration_ratio_wave_4_to_wave_2": _ratio(
                        durations["4"], durations["2"]
                    ),
                    "structure_alternates": (
                        str(waves["2"].get("structure", "")).lower()
                        != str(waves["4"].get("structure", "")).lower()
                    ),
                },
            }
        elif family in {"zigzag", "flat"} and all(position in children for position in ("A", "B", "C")):
            wave_a, wave_b, wave_c = (children[position] for position in ("A", "B", "C"))
            arithmetic_lengths = {
                "A": _price_length(wave_a),
                "B": _price_length(wave_b),
                "C": _price_length(wave_c),
            }
            logarithmic_lengths = {
                "A": _price_length(wave_a, logarithmic=True),
                "B": _price_length(wave_b, logarithmic=True),
                "C": _price_length(wave_c, logarithmic=True),
            }

            def correction_ratios(lengths: dict[str, float | None]) -> dict[str, float | None]:
                return {
                    "wave_B_retracement_of_A": _ratio(lengths["B"], lengths["A"]),
                    "wave_C_length_vs_A": _ratio(lengths["C"], lengths["A"]),
                }

            arithmetic_ratios = correction_ratios(arithmetic_lengths)
            logarithmic_ratios = correction_ratios(logarithmic_lengths)
            ratios = logarithmic_ratios if selected_scale == "logarithmic" else arithmetic_ratios
            item = {
                "parent_wave_id": parent.get("wave_id"),
                "family": family,
                "ratio_scale": selected_scale,
                "price_lengths": arithmetic_lengths,
                "log_price_lengths": logarithmic_lengths,
                "duration_days": {
                    "A": _duration_days(wave_a),
                    "B": _duration_days(wave_b),
                    "C": _duration_days(wave_c),
                },
                "ratios": ratios,
                "arithmetic_ratios": arithmetic_ratios,
                "logarithmic_ratios": logarithmic_ratios,
                "fibonacci_matches": {
                    key: _nearest_fibonacci(value, CORRECTION_FIBONACCI)
                    for key, value in ratios.items()
                },
            }
        elif family == "combination" and all(position in children for position in ("W", "X", "Y")):
            positions = ["W", "X", "Y"]
            if "X2" in children and "Z" in children:
                positions.extend(("X2", "Z"))
            arithmetic_lengths = {
                position: _price_length(children[position]) for position in positions
            }
            logarithmic_lengths = {
                position: _price_length(children[position], logarithmic=True)
                for position in positions
            }

            def combination_ratios(lengths: dict[str, float | None]) -> dict[str, float | None]:
                ratios = {
                    "wave_X_retracement_of_W": _ratio(lengths["X"], lengths["W"]),
                    "wave_Y_length_vs_W": _ratio(lengths["Y"], lengths["W"]),
                }
                if "X2" in lengths and "Z" in lengths:
                    ratios.update(
                        {
                            "wave_X2_retracement_of_Y": _ratio(
                                lengths["X2"], lengths["Y"]
                            ),
                            "wave_Z_length_vs_Y": _ratio(lengths["Z"], lengths["Y"]),
                            "wave_Z_length_vs_W": _ratio(lengths["Z"], lengths["W"]),
                        }
                    )
                return ratios

            arithmetic_ratios = combination_ratios(arithmetic_lengths)
            logarithmic_ratios = combination_ratios(logarithmic_lengths)
            ratios = logarithmic_ratios if selected_scale == "logarithmic" else arithmetic_ratios
            item = {
                "parent_wave_id": parent.get("wave_id"),
                "family": family,
                "combination_variant": (
                    "triple_three" if positions == ["W", "X", "Y", "X2", "Z"]
                    else "double_three"
                ),
                "ratio_scale": selected_scale,
                "price_lengths": arithmetic_lengths,
                "log_price_lengths": logarithmic_lengths,
                "duration_days": {
                    position: _duration_days(children[position])
                    for position in positions
                },
                "ratios": ratios,
                "arithmetic_ratios": arithmetic_ratios,
                "logarithmic_ratios": logarithmic_ratios,
                "fibonacci_matches": {
                    key: _nearest_fibonacci(value, CORRECTION_FIBONACCI)
                    for key, value in ratios.items()
                },
            }
        if item is not None:
            results.append(item)
    return results


def calculate_duration_prior_checks(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare observed elapsed time with the stored user ranges as soft flags."""
    nodes, node_by_id = _node_maps(response)
    results: list[dict[str, Any]] = []
    for node in nodes:
        duration_days = _duration_days(node)
        degree = str(node.get("degree") or "")
        position = str(node.get("sequence_position") or "")
        prior = DEGREE_DURATION_PRIORS.get(degree, {}).get(position)
        item: dict[str, Any] = {
            "wave_id": node.get("wave_id"),
            "degree": degree,
            "sequence_position": position,
            "wave_duration": _duration_prior_result(
                actual_hours=duration_days * DAY if duration_days is not None else None,
                prior=prior,
            ),
        }
        child_nodes = [
            node_by_id[child_id]
            for child_id in node.get("child_wave_ids", [])
            if child_id in node_by_id
        ]
        child_degrees = {str(child.get("degree")) for child in child_nodes}
        if child_nodes and len(child_degrees) == 1:
            child_degree = next(iter(child_degrees))
            total_prior = DEGREE_DURATION_PRIORS.get(child_degree, {}).get("total")
            item["child_sequence_duration"] = {
                "child_degree": child_degree,
                **_duration_prior_result(
                    actual_hours=duration_days * DAY if duration_days is not None else None,
                    prior=total_prior,
                ),
            }
        results.append(item)
    return results


def _leg_reference_levels(
    leg: dict[str, Any],
    *,
    base_price: float,
    levels: tuple[float, ...],
    retracement: bool,
) -> dict[str, dict[str, float] | None]:
    start = _number(_anchor(leg, "start", "price"))
    end = _number(_anchor(leg, "end", "price"))
    if start is None or end is None:
        return {"arithmetic": None, "logarithmic": None}
    direction = end - start
    sign = -1.0 if retracement else 1.0
    arithmetic = {
        f"{level:g}": base_price + sign * direction * level
        for level in levels
    }
    logarithmic: dict[str, float] | None = None
    if start > 0 and end > 0 and base_price > 0:
        log_direction = math.log(end / start)
        logarithmic = {
            f"{level:g}": base_price * math.exp(sign * log_direction * level)
            for level in levels
        }
    return {"arithmetic": arithmetic, "logarithmic": logarithmic}


def calculate_fibonacci_reference_levels(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Build retracement/extension references without turning them into trade targets."""
    nodes, node_by_id = _node_maps(response)
    results: list[dict[str, Any]] = []
    for parent in nodes:
        children = _children(parent, node_by_id)
        references: list[dict[str, Any]] = []

        def add_reference(
            name: str,
            leg_position: str,
            base_position: str,
            *,
            retracement: bool,
        ) -> None:
            leg = children.get(leg_position)
            base = children.get(base_position)
            base_price = _number(_anchor(base or {}, "end", "price"))
            if leg is None or base_price is None:
                return
            levels = RETRACEMENT_LEVELS if retracement else PROJECTION_LEVELS
            references.append(
                {
                    "name": name,
                    "based_on_wave_id": leg.get("wave_id"),
                    "origin_wave_id": base.get("wave_id") if base else None,
                    "kind": "retracement" if retracement else "extension",
                    "levels": _leg_reference_levels(
                        leg,
                        base_price=base_price,
                        levels=levels,
                        retracement=retracement,
                    ),
                }
            )

        if "1" in children:
            add_reference(
                "Wave 2 retracement from Wave 1",
                "1",
                "1",
                retracement=True,
            )
        if "1" in children and "2" in children:
            add_reference(
                "Wave 3 extension from Wave 2 using Wave 1",
                "1",
                "2",
                retracement=False,
            )
        if "3" in children:
            add_reference(
                "Wave 4 retracement from Wave 3",
                "3",
                "3",
                retracement=True,
            )
        if "1" in children and "4" in children:
            add_reference(
                "Wave 5 extension from Wave 4 using Wave 1",
                "1",
                "4",
                retracement=False,
            )
        if "A" in children:
            add_reference("Wave B retracement from A", "A", "A", retracement=True)
        if "A" in children and "B" in children:
            add_reference(
                "Wave C extension from B using A",
                "A",
                "B",
                retracement=False,
            )
        if "W" in children and "X" in children:
            add_reference(
                "Wave Y extension from X using W",
                "W",
                "X",
                retracement=False,
            )
        if "Y" in children and "X2" in children:
            add_reference(
                "Wave Z extension from X2 using Y",
                "Y",
                "X2",
                retracement=False,
            )
        if references:
            results.append(
                {
                    "parent_wave_id": parent.get("wave_id"),
                    "selected_scale": _ratio_scale(parent),
                    "references": references,
                    "policy": "Analytical count-validation references only; not entries, exits, or trade targets.",
                }
            )
    return results


def _point(node: dict[str, Any], *, logarithmic: bool) -> tuple[float, float] | None:
    date = parse_date(_anchor(node, "end", "date"))
    price = _number(_anchor(node, "end", "price"))
    if date is None or price is None or (logarithmic and price <= 0):
        return None
    x = date.timestamp() / 86_400.0
    return x, math.log(price) if logarithmic else price


def _line_value(point_1: tuple[float, float], point_2: tuple[float, float], x: float) -> float | None:
    if point_2[0] == point_1[0]:
        return None
    slope = (point_2[1] - point_1[1]) / (point_2[0] - point_1[0])
    return point_1[1] + slope * (x - point_1[0])


def _channel_for_scale(waves: dict[str, dict[str, Any]], logarithmic: bool) -> dict[str, Any] | None:
    points = {number: _point(waves[number], logarithmic=logarithmic) for number in waves}
    if any(points[number] is None for number in ("1", "2", "3", "4", "5")):
        return None
    p1, p2, p3, p4, p5 = (points[number] for number in ("1", "2", "3", "4", "5"))
    base_13_at_2 = _line_value(p1, p3, p2[0])
    base_13_at_4 = _line_value(p1, p3, p4[0])
    base_24_at_1 = _line_value(p2, p4, p1[0])
    base_24_at_5 = _line_value(p2, p4, p5[0])
    if None in (base_13_at_2, base_13_at_4, base_24_at_1, base_24_at_5):
        return None
    first_parallel_offset = p2[1] - float(base_13_at_2)
    expected_wave_4 = float(base_13_at_4) + first_parallel_offset
    second_parallel_offset = p1[1] - float(base_24_at_1)
    expected_wave_5 = float(base_24_at_5) + second_parallel_offset
    if logarithmic:
        wave_4_deviation_pct = (math.exp(p4[1] - expected_wave_4) - 1.0) * 100.0
        wave_5_deviation_pct = (math.exp(p5[1] - expected_wave_5) - 1.0) * 100.0
        expected_wave_4_price = math.exp(expected_wave_4)
        expected_wave_5_price = math.exp(expected_wave_5)
    else:
        expected_wave_4_price = expected_wave_4
        expected_wave_5_price = expected_wave_5
        wave_4_deviation_pct = (p4[1] / expected_wave_4 - 1.0) * 100.0 if expected_wave_4 else None
        wave_5_deviation_pct = (p5[1] / expected_wave_5 - 1.0) * 100.0 if expected_wave_5 else None
    return {
        "scale": "logarithmic" if logarithmic else "arithmetic",
        "wave_4_expected_on_1_3_parallel_through_2": expected_wave_4_price,
        "wave_4_channel_deviation_pct": wave_4_deviation_pct,
        "wave_5_expected_on_2_4_parallel_through_1": expected_wave_5_price,
        "wave_5_channel_deviation_pct": wave_5_deviation_pct,
        "combined_absolute_deviation_pct": (
            abs(float(wave_4_deviation_pct or 0.0))
            + abs(float(wave_5_deviation_pct or 0.0))
        ),
    }


def calculate_elliott_channels(response: dict[str, Any]) -> list[dict[str, Any]]:
    nodes, node_by_id = _node_maps(response)
    results: list[dict[str, Any]] = []
    for parent in nodes:
        if _family(parent.get("structure")) not in {"impulse", "diagonal"}:
            continue
        children = _children(parent, node_by_id)
        if not all(str(number) in children for number in range(1, 6)):
            continue
        waves = {str(number): children[str(number)] for number in range(1, 6)}
        arithmetic = _channel_for_scale(waves, False)
        logarithmic = _channel_for_scale(waves, True)
        available = [item for item in (arithmetic, logarithmic) if item is not None]
        preferred = (
            min(available, key=lambda item: item["combined_absolute_deviation_pct"])["scale"]
            if available
            else "undetermined"
        )
        results.append(
            {
                "parent_wave_id": parent.get("wave_id"),
                "preferred_channel_scale": preferred,
                "selection_rule": "Lower combined absolute Wave 4 and Wave 5 channel deviation.",
                "arithmetic": arithmetic,
                "logarithmic": logarithmic,
            }
        )
    return results


def _extreme(values: list[float], mode: str) -> float | None:
    if not values:
        return None
    return max(values) if mode == "max" else min(values)


def _pivot_alignment(
    raw_date: Any, raw_price: Any, candle: dict[str, Any] | None
) -> dict[str, Any]:
    expected = parse_date(raw_date)
    actual = candle.get("parsed_date") if isinstance(candle, dict) else None
    pivot_price = _number(raw_price)
    if expected is None or actual is None or pivot_price is None:
        return {
            "status": "unavailable",
            "wave_pivot_date": raw_date,
            "wave_pivot_price": raw_price,
            "indicator_bar_date": candle.get("date") if isinstance(candle, dict) else None,
        }
    date_only = isinstance(raw_date, str) and len(raw_date.strip()) == 10
    date_aligned = expected.date() == actual.date() if date_only else expected == actual
    candle_prices = [
        _number(candle.get(field)) for field in ("high", "low", "close")
    ]
    tolerance = max(1e-8, abs(pivot_price) * 1e-6)
    price_aligned = any(
        value is not None and abs(float(value) - pivot_price) <= tolerance
        for value in candle_prices
    )
    return {
        "status": "aligned" if date_aligned and price_aligned else "not_aligned",
        "wave_pivot_date": raw_date,
        "wave_pivot_price": pivot_price,
        "indicator_bar_date": candle.get("date"),
        "date_aligned": date_aligned,
        "price_aligned": price_aligned,
    }


def aggregate_wave_features(
    response: dict[str, Any],
    market_paths: Iterable[Path],
    *,
    workspace: Path,
    features: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    nodes, node_by_id = _node_maps(response)
    active_features = set(normalize_features(features))
    paths_by_timeframe: dict[str, Path] = {}
    for raw_path in market_paths:
        path = Path(raw_path).resolve()
        paths_by_timeframe.setdefault(infer_timeframe(path), path)
    cache: dict[Path, list[dict[str, Any]]] = {}
    metadata_cache: dict[Path, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for node in nodes:
        timeframe = str(node.get("timeframe") or "unknown")
        path = paths_by_timeframe.get(timeframe)
        item: dict[str, Any] = {
            "wave_id": node.get("wave_id"),
            "degree": node.get("degree"),
            "timeframe": timeframe,
            "status": "insufficient_data",
        }
        if path is None:
            item["reason"] = f"No {timeframe} candle file is available."
            results.append(item)
            continue
        if path not in cache:
            cache[path] = enrich_candles(load_candles(path), features=features)
            metadata_cache[path] = load_market_metadata(path)
        start = parse_date(_anchor(node, "start", "date"))
        end = parse_date(_anchor(node, "end", "date"))
        candles = cache[path]
        if start is None:
            item["reason"] = "Wave start date is unavailable."
            results.append(item)
            continue
        effective_end = end or candles[-1].get("parsed_date")
        if effective_end is None:
            item["reason"] = "Wave end and latest candle dates are unavailable."
            results.append(item)
            continue
        subset = [
            row
            for row in candles
            if row.get("parsed_date") is not None
            and start <= row["parsed_date"] <= effective_end
        ]
        if not subset:
            item["reason"] = "No candles fall inside the wave boundaries."
            results.append(item)
            continue
        def values(field: str) -> list[float]:
            return [float(row[field]) for row in subset if row.get(field) is not None]
        volumes = values("volume")
        volume_ratios = values("volume_ratio_50")
        ewo = values("ewo")
        normalized_ewo = values("ewo_normalized_pct")
        macd = values("macd")
        hist = values("macd_histogram")
        atr_pct = values("atr_pct")
        bb_positions = values("bollinger_position")
        kc_positions = values("keltner_position")
        endpoint = subset[-1]
        first_close = float(subset[0]["close"])
        last_close = float(endpoint["close"])
        try:
            source = str(path.relative_to(workspace.resolve()))
        except ValueError:
            source = path.name
        item.update(
            {
                "status": "calculated",
                "source": source,
                "measurement_policy": "Endpoint and within-wave extremes use one unchanged timeframe/feed.",
                "bar_count": len(subset),
                "calendar_days": (
                    (effective_end - start).total_seconds() / 86_400.0
                    if effective_end is not None
                    else None
                ),
                "price": {
                    "first_close": first_close,
                    "last_close": last_close,
                    "return_pct": (last_close / first_close - 1.0) * 100.0 if first_close else None,
                    "log_return": math.log(last_close / first_close) if first_close > 0 and last_close > 0 else None,
                    "highest_high": max(float(row["high"]) for row in subset),
                    "lowest_low": min(float(row["low"]) for row in subset),
                },
                "volume": {
                    "cumulative": sum(volumes) if volumes else None,
                    "average": sum(volumes) / len(volumes) if volumes else None,
                    "average_ratio_to_50_period": (
                        sum(volume_ratios) / len(volume_ratios) if volume_ratios else None
                    ),
                    "peak_ratio_to_50_period": max(volume_ratios, default=None),
                },
                "ewo": {
                    "endpoint": endpoint.get("ewo"),
                    "maximum": _extreme(ewo, "max"),
                    "minimum": _extreme(ewo, "min"),
                    "absolute_peak": max((abs(value) for value in ewo), default=None),
                    "normalized_endpoint_pct": endpoint.get("ewo_normalized_pct"),
                    "normalized_absolute_peak_pct": max(
                        (abs(value) for value in normalized_ewo), default=None
                    ),
                    "zero_touch": min(ewo) <= 0 <= max(ewo) if ewo else None,
                },
                "macd": {
                    "endpoint": endpoint.get("macd"),
                    "signal_endpoint": endpoint.get("macd_signal"),
                    "histogram_endpoint": endpoint.get("macd_histogram"),
                    "maximum": _extreme(macd, "max"),
                    "minimum": _extreme(macd, "min"),
                    "histogram_positive_peak": max(hist, default=None),
                    "histogram_negative_peak": min(hist, default=None),
                },
                "volatility": {
                    "atr_pct_endpoint": endpoint.get("atr_pct"),
                    "atr_pct_average": sum(atr_pct) / len(atr_pct) if atr_pct else None,
                    "bollinger_outer_pierces": sum(value < 0 or value > 1 for value in bb_positions),
                    "keltner_outer_pierces": sum(value < 0 or value > 1 for value in kc_positions),
                },
            }
        )
        if "rsi" in active_features:
            rsi_metrics = summarize_rsi_window(
                [row.get("rsi") for row in subset],
                [str(row.get("rsi_status") or "unknown") for row in subset],
            )
            rsi_metrics.update(
                {
                    "wave_id": node.get("wave_id"),
                    "start_pivot_alignment": _pivot_alignment(
                        _anchor(node, "start", "date"),
                        _anchor(node, "start", "price"),
                        subset[0],
                    ),
                    "pivot_alignment": _pivot_alignment(
                        _anchor(node, "end", "date"),
                        _anchor(node, "end", "price"),
                        subset[-1],
                    ),
                    "start_pivot_price": _number(_anchor(node, "start", "price")),
                    "end_pivot_price": _number(_anchor(node, "end", "price")),
                    "source": build_indicator_source_metadata(
                        path,
                        metadata_cache[path],
                        timeframe=timeframe,
                    ),
                }
            )
            item["rsi"] = rsi_metrics
        results.append(item)

    if "rsi" in active_features:
        result_by_id = {
            str(item.get("wave_id")): item
            for item in results
            if isinstance(item.get("rsi"), dict)
        }
        for item in result_by_id.values():
            node = node_by_id.get(str(item.get("wave_id")), {})
            comparison_wave_id = node.get("rsi_comparison_wave_id")
            if not comparison_wave_id:
                continue
            prior_item = result_by_id.get(str(comparison_wave_id))
            if prior_item is None:
                comparison = {
                    "prior_wave_id": comparison_wave_id,
                    "comparability": "unavailable",
                    "divergence_candidate": None,
                    "divergence_strength_rsi_points": None,
                    "divergence_distance": None,
                    "evidence": indicator_evidence(
                        "unavailable",
                        "The explicit RSI comparison wave could not be measured.",
                        unavailable_or_incomparable=(
                            "The comparison wave has no matching same-timeframe RSI evidence.",
                        ),
                    ),
                }
            else:
                comparison = compare_rsi_pivots(item["rsi"], prior_item["rsi"])
            item["rsi"]["comparison_with_prior_wave"] = comparison
            for field in (
                "comparability",
                "divergence_candidate",
                "divergence_strength_rsi_points",
                "divergence_distance",
                "evidence",
            ):
                item["rsi"][field] = comparison.get(field)
    return results


def calculate_resolution_analytics(
    response: dict[str, Any],
    market_paths: Iterable[Path],
    *,
    workspace: Path,
    features: Iterable[str] | None = None,
    origin_run_id: int | None = None,
) -> dict[str, Any]:
    active_features = normalize_features(features)
    resolved_paths = [Path(path).resolve() for path in market_paths]
    return {
        "active_candle_features": list(active_features),
        "wave_features": aggregate_wave_features(
            response,
            resolved_paths,
            workspace=workspace,
            features=active_features,
        ),
        "wave_fingerprint_analysis": generate_response_fingerprints(
            response,
            resolved_paths,
            workspace=workspace,
            features=active_features,
            origin_run_id=origin_run_id,
        ),
        "fibonacci_duration_alternation": calculate_fibonacci_and_duration(response),
        "fibonacci_reference_levels": calculate_fibonacci_reference_levels(response),
        "duration_prior_checks": calculate_duration_prior_checks(response),
        "elliott_channels": calculate_elliott_channels(response),
        "policy": (
            "These calculations rank price-valid counts. They do not create pivots, "
            "override hard Elliott rules, or provide entries/exits."
        ),
    }
