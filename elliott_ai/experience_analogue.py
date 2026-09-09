"""Endpoint-only deterministic analogue comparisons for reviewed experiences.

Phase 5A.3 compares cases that already passed the Phase 5A.2 structural
filter. It exposes per-dimension differences, never a prediction, probability,
wave decision, trade decision, or cross-case rank.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from .correction_semantics import combination_variant, structure_family
from .experience_comparison import (
    COMPARISON_SCHEMA_VERSION,
    endpoint_descriptor,
    family_compatibility,
    role_compatibility,
    validate_endpoint_dna,
)
from .schema import STANDARD_DEGREES


ANALOGUE_COMPARISON_SCHEMA_VERSION = "experience-analogue-comparison-1.0.0"
ANALOGUE_COMPARISON_CALCULATION_VERSION = (
    "experience-analogue-comparison-calc-1.0.0"
)
COMPARISON_SPECIFICATION_VERSION = "experience-analogue-spec-1.0.0"
DIMENSION_SPECIFICATION_VERSION = "experience-analogue-dimensions-1.0.0"

DIMENSION_STATES = (
    "comparable",
    "missing_current",
    "missing_historical",
    "missing_both",
    "structurally_inapplicable",
    "feed_incomparable",
    "provenance_incomparable",
    "unsupported_by_spec",
)

COMPARISON_METHODS = (
    "descriptive_pair",
    "numeric_absolute_signed",
    "numeric_ratio",
    "numeric_tolerance_band",
    "categorical_exact",
    "categorical_compatibility",
    "ordered_state_distance",
)

MISSING_STATES = frozenset(
    {"missing_current", "missing_historical", "missing_both"}
)
INCOMPARABLE_STATES = frozenset(
    {"feed_incomparable", "provenance_incomparable"}
)
OMITTED_STATES = frozenset(
    {"structurally_inapplicable", "unsupported_by_spec"}
)


@dataclass(frozen=True)
class DimensionDefinition:
    dimension_id: str
    name: str
    group: str
    source_field: str
    data_type: str
    comparison_method: str
    normalization_rule: str
    missing_data_behavior: str
    incomparability_behavior: str
    tolerance_rule: str
    output_format: str
    distance_producing: bool
    specification_version: str = DIMENSION_SPECIFICATION_VERSION
    allowed_levels: tuple[int, ...] = (1, 2, 3, 4)
    requirements: tuple[str, ...] = ()
    tolerance_metric: str | None = None
    tolerance: float | None = None
    compatibility_domain: str | None = None
    ordered_values: tuple[str, ...] = ()
    incomparable_tags: tuple[str, ...] = ()
    derivation: str | None = None


def _dimension(
    dimension_id: str,
    name: str,
    group: str,
    source_field: str,
    data_type: str,
    comparison_method: str,
    normalization_rule: str,
    *,
    allowed_levels: Sequence[int] = (1, 2, 3, 4),
    requirements: Sequence[str] = (),
    tolerance_metric: str | None = None,
    tolerance: float | None = None,
    compatibility_domain: str | None = None,
    ordered_values: Sequence[str] = (),
    incomparable_tags: Sequence[str] = (),
    derivation: str | None = None,
    distance_producing: bool = True,
    output_format: str = "structured_json",
) -> DimensionDefinition:
    tolerance_rule = (
        f"Inclusive {tolerance_metric} <= {tolerance}."
        if tolerance_metric is not None and tolerance is not None
        else "No numerical tolerance; report the explicit comparison result."
    )
    return DimensionDefinition(
        dimension_id=dimension_id,
        name=name,
        group=group,
        source_field=source_field,
        data_type=data_type,
        comparison_method=comparison_method,
        normalization_rule=normalization_rule,
        missing_data_behavior=(
            "Return missing_current, missing_historical, or missing_both; never impute."
        ),
        incomparability_behavior=(
            "Return the exact feed/provenance/inapplicability state; never substitute zero."
        ),
        tolerance_rule=tolerance_rule,
        output_format=output_format,
        distance_producing=distance_producing,
        allowed_levels=tuple(int(level) for level in allowed_levels),
        requirements=tuple(str(item) for item in requirements),
        tolerance_metric=tolerance_metric,
        tolerance=tolerance,
        compatibility_domain=compatibility_domain,
        ordered_values=tuple(str(item) for item in ordered_values),
        incomparable_tags=tuple(str(item) for item in incomparable_tags),
        derivation=derivation,
    )


_DEGREES = tuple(degree for degree in STANDARD_DEGREES if degree != "Unassigned")


DIMENSION_DEFINITIONS: tuple[DimensionDefinition, ...] = (
    _dimension(
        "endpoint_position",
        "Endpoint wave position",
        "endpoint_identity_role",
        "groups.structural_context.values.candidate_role",
        "category",
        "categorical_exact",
        "Upper-case Elliott position after removing parentheses.",
        distance_producing=False,
    ),
    _dimension(
        "structural_role_class",
        "Explicit structural role class",
        "endpoint_identity_role",
        "descriptor.role.role_class",
        "category",
        "categorical_compatibility",
        "Canonical Phase 5A.2 role class.",
        compatibility_domain="role",
        distance_producing=False,
    ),
    _dimension(
        "parent_family",
        "Parent pattern family",
        "endpoint_identity_role",
        "descriptor.family_class",
        "category",
        "categorical_compatibility",
        "Canonical Elliott family spelling.",
        compatibility_domain="family",
        distance_producing=False,
    ),
    _dimension(
        "direction",
        "Wave direction",
        "endpoint_identity_role",
        "groups.price_shape.values.direction",
        "category",
        "categorical_exact",
        "Lower-case direction category.",
        distance_producing=False,
    ),
    _dimension(
        "elliott_degree",
        "Elliott degree",
        "endpoint_identity_role",
        "descriptor.elliott_degree",
        "ordered_category",
        "ordered_state_distance",
        "Case-insensitive standard Elliott degree order.",
        ordered_values=_DEGREES,
    ),
    _dimension(
        "timeframe",
        "Source timeframe",
        "endpoint_identity_role",
        "descriptor.source.timeframe",
        "category",
        "categorical_exact",
        "Lower-case timeframe identity.",
        allowed_levels=(1, 2, 3),
        distance_producing=False,
    ),
    _dimension(
        "symbol",
        "Instrument symbol",
        "endpoint_identity_role",
        "groups.source_identity.values.source.symbol",
        "category",
        "descriptive_pair",
        "Upper-case exchange-qualified symbol.",
        allowed_levels=(1, 2, 3),
        distance_producing=False,
    ),
    _dimension(
        "provider",
        "Market-data provider",
        "endpoint_identity_role",
        "descriptor.source.provider",
        "category",
        "descriptive_pair",
        "Lower-case provider identity.",
        allowed_levels=(1, 2, 3),
        distance_producing=False,
    ),
    _dimension(
        "feed_identity",
        "Feed identity",
        "endpoint_identity_role",
        "descriptor.source.feed_identity",
        "category",
        "descriptive_pair",
        "Exact immutable feed identity.",
        allowed_levels=(1, 2, 3),
        distance_producing=False,
    ),
    _dimension(
        "market_type",
        "Market type",
        "endpoint_identity_role",
        "descriptor.source.market_type",
        "category",
        "categorical_exact",
        "Lower-case market or asset type.",
        distance_producing=False,
    ),
    _dimension(
        "percentage_change",
        "Signed percentage change",
        "structural_geometry",
        "groups.price_shape.values.percentage_change",
        "number",
        "numeric_absolute_signed",
        "Already normalized as percent of wave start price.",
        tolerance_metric="absolute_difference",
        tolerance=5.0,
    ),
    _dimension(
        "log_return",
        "Log price return",
        "structural_geometry",
        "groups.price_shape.values.log_return",
        "number",
        "numeric_absolute_signed",
        "Natural-log return is dimensionless.",
        tolerance_metric="absolute_difference",
        tolerance=0.05,
    ),
    _dimension(
        "normalized_range_pct",
        "Normalized price range",
        "structural_geometry",
        "groups.price_shape.values.normalized_range_pct",
        "number",
        "numeric_tolerance_band",
        "Price range as percent of wave start price.",
        tolerance_metric="absolute_difference",
        tolerance=5.0,
    ),
    _dimension(
        "directional_efficiency",
        "Directional efficiency",
        "structural_geometry",
        "groups.price_shape.values.directional_efficiency",
        "number",
        "numeric_tolerance_band",
        "Unitless signed-path efficiency in the source fingerprint.",
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "duration_candles",
        "Wave duration in candles",
        "structural_geometry",
        "groups.duration.values.candles",
        "number",
        "numeric_ratio",
        "Raw candle count, permitted only on the same timeframe.",
        allowed_levels=(1, 2, 3),
        requirements=("same_timeframe",),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.25,
        incomparable_tags=("duration.raw_candles",),
    ),
    _dimension(
        "duration_clock_days",
        "Wave duration in clock days",
        "structural_geometry",
        "groups.duration.values.clock_days",
        "number",
        "numeric_ratio",
        "Elapsed UTC clock days.",
        allowed_levels=(1, 2, 3),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.25,
    ),
    _dimension(
        "retracement_ratio",
        "Retracement ratio",
        "structural_geometry",
        "groups.fibonacci_relationships.values.retracement.value",
        "number",
        "numeric_tolerance_band",
        "Dimensionless ratio from the referenced Phase 3 sibling wave.",
        tolerance_metric="absolute_difference",
        tolerance=0.05,
    ),
    _dimension(
        "extension_ratio",
        "Extension ratio",
        "structural_geometry",
        "groups.fibonacci_relationships.values.extension.value",
        "number",
        "numeric_tolerance_band",
        "Dimensionless ratio from the referenced Phase 3 sibling wave.",
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "overlap_ratio",
        "Price-range overlap ratio",
        "structural_geometry",
        "groups.fibonacci_relationships.values.overlap.value",
        "number",
        "numeric_tolerance_band",
        "Dimensionless overlap ratio.",
        tolerance_metric="absolute_difference",
        tolerance=0.05,
    ),
    _dimension(
        "pivot_count",
        "Internal pivot count",
        "structural_geometry",
        "groups.pivot_path.values.pivot_count",
        "number",
        "numeric_tolerance_band",
        "Integer count on the same source timeframe.",
        allowed_levels=(1, 2, 3),
        requirements=("same_timeframe",),
        tolerance_metric="absolute_difference",
        tolerance=1.0,
    ),
    _dimension(
        "internal_child_count",
        "Internal child-wave count",
        "structural_geometry",
        "groups.structural_context.values.internal_child_count",
        "number",
        "numeric_tolerance_band",
        "Integer structural child count.",
        tolerance_metric="absolute_difference",
        tolerance=0.0,
    ),
    _dimension(
        "internal_structure_family",
        "Internal structure family",
        "structural_geometry",
        "groups.structural_context.values.internal_structure_family",
        "category",
        "categorical_compatibility",
        "Canonical Elliott family spelling.",
        compatibility_domain="family",
        distance_producing=False,
    ),
    _dimension(
        "acceleration_state",
        "Price acceleration state",
        "structural_geometry",
        "groups.price_shape.values.acceleration.state",
        "ordered_category",
        "ordered_state_distance",
        "Ordered as decelerating, stable, accelerating.",
        ordered_values=("decelerating", "stable", "accelerating"),
    ),
    _dimension(
        "channel_fit_r_squared",
        "Channel fit R-squared",
        "structural_geometry",
        "groups.channel_behaviour.values.fit_r_squared",
        "number",
        "numeric_tolerance_band",
        "Unitless OLS fit statistic.",
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "channel_terminal_deviation_sigma",
        "Terminal channel deviation",
        "structural_geometry",
        "groups.channel_behaviour.values.terminal_deviation_sigma",
        "number",
        "numeric_tolerance_band",
        "Standard-deviation units from the cutoff-safe channel fit.",
        tolerance_metric="absolute_difference",
        tolerance=0.50,
    ),
    _dimension(
        "ewo_absolute_peak",
        "EWO absolute peak",
        "momentum_context",
        "groups.momentum.values.ewo.absolute_peak",
        "number",
        "numeric_ratio",
        "Raw price-unit momentum; same feed and timeframe only.",
        allowed_levels=(1, 3),
        requirements=("same_feed", "same_timeframe"),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.25,
        incomparable_tags=("momentum.raw_ewo",),
    ),
    _dimension(
        "ewo_peak_timing",
        "EWO absolute-peak timing",
        "momentum_context",
        "groups.momentum.values.ewo.absolute_peak_timing",
        "number",
        "numeric_tolerance_band",
        "Position from 0 to 1 within each wave.",
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "ewo_zero_crossings",
        "EWO zero-line crossings",
        "momentum_context",
        "groups.momentum.values.ewo.zero_line_crossings",
        "number",
        "numeric_tolerance_band",
        "Integer count; same timeframe only.",
        allowed_levels=(1, 2, 3),
        requirements=("same_timeframe",),
        tolerance_metric="absolute_difference",
        tolerance=1.0,
    ),
    _dimension(
        "macd_absolute_peak",
        "MACD absolute peak",
        "momentum_context",
        "groups.momentum.values.macd.absolute_peak",
        "number",
        "numeric_ratio",
        "Raw price-unit momentum; same feed and timeframe only.",
        allowed_levels=(1, 3),
        requirements=("same_feed", "same_timeframe"),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.25,
        incomparable_tags=("momentum.raw_macd",),
    ),
    _dimension(
        "macd_peak_timing",
        "MACD absolute-peak timing",
        "momentum_context",
        "groups.momentum.values.macd.absolute_peak_timing",
        "number",
        "numeric_tolerance_band",
        "Position from 0 to 1 within each wave.",
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "macd_histogram_state",
        "MACD histogram state",
        "momentum_context",
        "groups.momentum.values.macd.histogram_state",
        "ordered_category",
        "ordered_state_distance",
        "Ordered as contracting, stable, expanding.",
        ordered_values=("contracting", "stable", "expanding"),
    ),
    _dimension(
        "rsi_end_value",
        "RSI value at wave end",
        "momentum_context",
        "groups.optional_rsi.values.end_value",
        "number",
        "numeric_tolerance_band",
        "Canonical Wilder RSI on a 0-100 scale; same feed and timeframe only.",
        allowed_levels=(1, 2, 3),
        requirements=("same_feed", "same_timeframe"),
        tolerance_metric="absolute_difference",
        tolerance=5.0,
    ),
    _dimension(
        "rsi_slope",
        "RSI slope per candle",
        "momentum_context",
        "groups.optional_rsi.values.slope_per_bar",
        "number",
        "numeric_tolerance_band",
        "RSI points per candle; same feed and timeframe only.",
        allowed_levels=(1, 2, 3),
        requirements=("same_feed", "same_timeframe"),
        tolerance_metric="absolute_difference",
        tolerance=0.25,
    ),
    _dimension(
        "rsi_divergence_state",
        "Aligned RSI divergence state",
        "momentum_context",
        "groups.optional_rsi.values.aligned_divergence_candidate",
        "category",
        "categorical_exact",
        "Source fingerprint result from aligned price pivots only.",
        allowed_levels=(1, 2, 3),
        requirements=("same_feed", "same_timeframe"),
        distance_producing=False,
    ),
    _dimension(
        "rsi_proportion_above_50",
        "RSI proportion above 50",
        "momentum_context",
        "groups.optional_rsi.values.proportion_above_50",
        "number",
        "numeric_tolerance_band",
        "Proportion from 0 to 1; same feed and timeframe only.",
        allowed_levels=(1, 2, 3),
        requirements=("same_feed", "same_timeframe"),
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "volume_average_to_baseline",
        "Average volume relative to baseline",
        "volume_context",
        "groups.volume.values.average_ratio_to_baseline",
        "number",
        "numeric_ratio",
        "Per-feed normalized ratio; equal volume scope required.",
        requirements=("same_volume_scope",),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.20,
    ),
    _dimension(
        "volume_terminal_to_median",
        "Terminal volume relative to wave median",
        "volume_context",
        "groups.volume.values.terminal_volume_ratio_to_wave_median",
        "number",
        "numeric_ratio",
        "Within-wave normalized ratio; equal volume scope required.",
        requirements=("same_volume_scope",),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.20,
    ),
    _dimension(
        "volume_peak_location",
        "Peak-volume location",
        "volume_context",
        "groups.volume.values.peak_volume_location",
        "number",
        "numeric_tolerance_band",
        "Position from 0 to 1 within each wave; equal volume scope required.",
        requirements=("same_volume_scope",),
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "volume_effort_vs_result",
        "Volume effort versus price result",
        "volume_context",
        "groups.volume.values.effort_vs_result",
        "number",
        "numeric_ratio",
        "Normalized volume divided by absolute percentage move; equal volume scope required.",
        requirements=("same_volume_scope",),
        tolerance_metric="absolute_ratio_delta",
        tolerance=0.25,
    ),
    _dimension(
        "volume_proportion_above_baseline",
        "Volume proportion above baseline",
        "volume_context",
        "groups.volume.values.proportion_above_baseline",
        "number",
        "numeric_tolerance_band",
        "Proportion from 0 to 1; equal volume scope required.",
        requirements=("same_volume_scope",),
        tolerance_metric="absolute_difference",
        tolerance=0.10,
    ),
    _dimension(
        "volume_participation_regime",
        "Derived volume participation regime",
        "volume_context",
        "groups.volume.values.average_ratio_to_baseline",
        "category",
        "ordered_state_distance",
        "Below 0.8 contracting, 0.8-1.2 balanced, above 1.2 expanding.",
        requirements=("same_volume_scope",),
        ordered_values=("contracting", "balanced", "expanding"),
        derivation="volume_participation_regime_v1",
    ),
    _dimension(
        "volatility_state",
        "ATR volatility state",
        "volatility_context",
        "groups.volatility.values.volatility_state",
        "ordered_category",
        "ordered_state_distance",
        "Ordered as contracting, stable, expanding.",
        ordered_values=("contracting", "stable", "expanding"),
    ),
    _dimension(
        "atr_pct_mean",
        "Mean ATR as percent of price",
        "volatility_context",
        "groups.volatility.values.atr_pct_mean",
        "number",
        "numeric_tolerance_band",
        "ATR normalized as percent of price.",
        tolerance_metric="absolute_difference",
        tolerance=0.50,
    ),
    _dimension(
        "bollinger_outer_pierces",
        "Bollinger outer-band pierces",
        "volatility_context",
        "groups.volatility.values.bollinger_outer_pierces",
        "number",
        "numeric_tolerance_band",
        "Integer count on the source wave.",
        allowed_levels=(1, 2, 3),
        requirements=("same_timeframe",),
        tolerance_metric="absolute_difference",
        tolerance=1.0,
    ),
    _dimension(
        "keltner_outer_pierces",
        "Keltner outer-band pierces",
        "volatility_context",
        "groups.volatility.values.keltner_outer_pierces",
        "number",
        "numeric_tolerance_band",
        "Integer count on the source wave.",
        allowed_levels=(1, 2, 3),
        requirements=("same_timeframe",),
        tolerance_metric="absolute_difference",
        tolerance=1.0,
    ),
    _dimension(
        "bollinger_terminal_position",
        "Terminal Bollinger position",
        "volatility_context",
        "groups.volatility.values.bollinger_terminal_position",
        "number",
        "numeric_tolerance_band",
        "Dimensionless position relative to the bands.",
        tolerance_metric="absolute_difference",
        tolerance=0.25,
    ),
    _dimension(
        "parent_wave_id",
        "Parent wave identifier",
        "multitimeframe_context",
        "groups.structural_context.values.parent_wave_id",
        "category",
        "descriptive_pair",
        "Exact source identifier; descriptive only.",
        allowed_levels=(1, 2, 3),
        distance_producing=False,
    ),
    _dimension(
        "broader_regime",
        "Broader market regime",
        "multitimeframe_context",
        "groups.market_context.values.broader_regime",
        "category",
        "categorical_exact",
        "Lower-case reviewed-at-cutoff regime when available.",
        distance_producing=False,
    ),
    _dimension(
        "session",
        "Trading session",
        "market_context",
        "groups.market_context.values.session",
        "category",
        "categorical_exact",
        "Lower-case session identity.",
        allowed_levels=(1, 2, 3),
        distance_producing=False,
    ),
    _dimension(
        "venue",
        "Trading venue",
        "market_context",
        "groups.market_context.values.venue",
        "category",
        "descriptive_pair",
        "Upper-case venue identity.",
        distance_producing=False,
    ),
    _dimension(
        "quote_currency",
        "Quote currency",
        "market_context",
        "groups.market_context.values.quote_currency",
        "category",
        "categorical_exact",
        "Upper-case quote currency.",
        distance_producing=False,
    ),
    _dimension(
        "child_structure_status",
        "Child-structure validation at cutoff",
        "confirmation_at_cutoff",
        "groups.structural_context.values.child_structure_status",
        "category",
        "categorical_exact",
        "Lower-case source status at the endpoint cutoff.",
        distance_producing=False,
    ),
    _dimension(
        "completion_status_at_cutoff",
        "Wave completion status at cutoff",
        "confirmation_at_cutoff",
        "groups.structural_context.values.completion_status_at_cutoff",
        "category",
        "categorical_exact",
        "Lower-case completion state from the endpoint fingerprint.",
        distance_producing=False,
    ),
    _dimension(
        "channel_break_state_at_cutoff",
        "Channel terminal state at cutoff",
        "confirmation_at_cutoff",
        "groups.channel_behaviour.values.terminal_break_status",
        "category",
        "categorical_exact",
        "Lower-case cutoff-safe channel state.",
        distance_producing=False,
    ),
    _dimension(
        "start_pivot_alignment",
        "Start-pivot confirmation",
        "confirmation_at_cutoff",
        "groups.pivot_path.values.start_alignment.status",
        "category",
        "categorical_exact",
        "Lower-case price/candle alignment state.",
        distance_producing=False,
    ),
    _dimension(
        "end_pivot_alignment",
        "End-pivot confirmation",
        "confirmation_at_cutoff",
        "groups.pivot_path.values.end_alignment.status",
        "category",
        "categorical_exact",
        "Lower-case price/candle alignment state.",
        distance_producing=False,
    ),
)


DIMENSION_BY_ID = {
    definition.dimension_id: definition for definition in DIMENSION_DEFINITIONS
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


def analogue_comparison_content_hash(value: Mapping[str, Any]) -> str:
    return _hash_payload(value)


def _specification_payload() -> dict[str, Any]:
    return {
        "specification_version": COMPARISON_SPECIFICATION_VERSION,
        "dimension_specification_version": DIMENSION_SPECIFICATION_VERSION,
        "comparison_schema_version": ANALOGUE_COMPARISON_SCHEMA_VERSION,
        "calculation_version": ANALOGUE_COMPARISON_CALCULATION_VERSION,
        "phase5a2_filter_schema_version": COMPARISON_SCHEMA_VERSION,
        "dimension_states": list(DIMENSION_STATES),
        "comparison_methods": list(COMPARISON_METHODS),
        "dimensions": [asdict(item) for item in DIMENSION_DEFINITIONS],
        "group_distance_policy": (
            "No grouped or overall distance is calculated in Phase 5A.3; only state counts "
            "and transparent per-dimension differences are emitted."
        ),
        "ordering_policy": (
            "Stable Phase 5A.2 identity order only; no similarity or outcome rank."
        ),
        "outcome_policy": (
            "Confirmation DNA and reviewed outcome DNA are forbidden comparison inputs."
        ),
    }


def comparison_specification(
    version: str = COMPARISON_SPECIFICATION_VERSION,
) -> dict[str, Any]:
    if version != COMPARISON_SPECIFICATION_VERSION:
        raise ValueError(
            f"Unsupported analogue comparison specification {version!r}; "
            f"supported: {COMPARISON_SPECIFICATION_VERSION}."
        )
    payload = _specification_payload()
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _field(value: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _normalize_family(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    family = structure_family(text)
    if family == "combination":
        return (
            "triple_three" if combination_variant(text) == "triple" else "double_three"
        )
    normalized = family.replace("-", "_").replace(" ", "_")
    return normalized if normalized and normalized != "unknown" else None


def _normalize_category(value: Any, rule: str) -> tuple[bool, Any, str | None]:
    if not isinstance(value, str):
        return False, None, "A non-empty string category is required."
    text = value.strip()
    if not text:
        return False, None, "A non-empty string category is required."
    if "Elliott family" in rule or "Canonical Elliott family" in rule:
        family = _normalize_family(text)
        return (
            (True, family, None)
            if family is not None
            else (False, None, "The Elliott family is unsupported by this specification.")
        )
    if "Upper-case Elliott position" in rule:
        return True, text.replace("(", "").replace(")", "").upper(), None
    if "Upper-case" in rule:
        return True, text.upper(), None
    if "Case-insensitive standard Elliott degree" in rule:
        lookup = {item.lower(): item for item in _DEGREES}
        resolved = lookup.get(text.lower())
        return (
            (True, resolved, None)
            if resolved is not None
            else (False, None, "The Elliott degree is unsupported by this specification.")
        )
    if "Canonical Phase 5A.2 role class" in rule:
        return True, text.lower(), None
    if "Exact immutable feed identity" in rule:
        return True, text, None
    return True, text.lower(), None


def _normalize_value(
    definition: DimensionDefinition, value: Any
) -> tuple[bool, Any, str | None]:
    if definition.derivation == "volume_participation_regime_v1":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, None, "Volume participation derivation requires a finite number."
        number = float(value)
        if not math.isfinite(number):
            return False, None, "Volume participation derivation received a non-finite number."
        return (
            True,
            "contracting" if number < 0.8 else "expanding" if number > 1.2 else "balanced",
            None,
        )
    if definition.data_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, None, "A finite numeric value is required."
        number = float(value)
        if not math.isfinite(number):
            return False, None, "A finite numeric value is required; NaN and infinity are rejected."
        return True, number, None
    if definition.data_type in {"category", "ordered_category"}:
        return _normalize_category(value, definition.normalization_rule)
    return False, None, f"Unsupported data type {definition.data_type!r}."


def _tolerance_result(
    definition: DimensionDefinition,
    difference: Mapping[str, Any],
) -> dict[str, Any]:
    metric = definition.tolerance_metric
    threshold = definition.tolerance
    if metric is None or threshold is None:
        return {
            "status": "not_applicable",
            "metric": None,
            "observed": None,
            "threshold": None,
            "inclusive": True,
        }
    observed = difference.get(metric)
    if not isinstance(observed, (int, float)) or not math.isfinite(float(observed)):
        return {
            "status": "unavailable",
            "metric": metric,
            "observed": observed,
            "threshold": threshold,
            "inclusive": True,
        }
    return {
        "status": (
            "within_tolerance"
            if float(observed) <= float(threshold)
            else "outside_tolerance"
        ),
        "metric": metric,
        "observed": float(observed),
        "threshold": float(threshold),
        "inclusive": True,
    }


def compare_dimension_values(
    definition: DimensionDefinition,
    current_value: Any,
    historical_value: Any,
) -> dict[str, Any]:
    """Apply one explicit comparison method without feed or level assumptions."""
    current_ok, current_normalized, current_error = _normalize_value(
        definition, current_value
    )
    historical_ok, historical_normalized, historical_error = _normalize_value(
        definition, historical_value
    )
    if not current_ok or not historical_ok:
        return {
            "state": "unsupported_by_spec",
            "normalized_current_value": current_normalized,
            "normalized_historical_value": historical_normalized,
            "difference": None,
            "tolerance_band_result": {
                "status": "unavailable",
                "metric": definition.tolerance_metric,
                "observed": None,
                "threshold": definition.tolerance,
                "inclusive": True,
            },
            "reason": "; ".join(
                item for item in (current_error, historical_error) if item
            ),
        }

    method = definition.comparison_method
    difference: dict[str, Any]
    if method in {"numeric_absolute_signed", "numeric_tolerance_band"}:
        signed = float(current_normalized) - float(historical_normalized)
        difference = {
            "kind": method,
            "signed_difference": signed,
            "absolute_difference": abs(signed),
        }
    elif method == "numeric_ratio":
        reference = float(historical_normalized)
        if reference == 0.0:
            return {
                "state": "unsupported_by_spec",
                "normalized_current_value": current_normalized,
                "normalized_historical_value": historical_normalized,
                "difference": {
                    "kind": method,
                    "signed_difference": float(current_normalized) - reference,
                    "absolute_difference": abs(float(current_normalized) - reference),
                    "ratio": None,
                },
                "tolerance_band_result": {
                    "status": "unavailable",
                    "metric": definition.tolerance_metric,
                    "observed": None,
                    "threshold": definition.tolerance,
                    "inclusive": True,
                },
                "reason": "A ratio difference is undefined when the historical reference is zero.",
            }
        ratio = float(current_normalized) / reference
        difference = {
            "kind": method,
            "signed_difference": float(current_normalized) - reference,
            "absolute_difference": abs(float(current_normalized) - reference),
            "ratio": ratio,
            "signed_ratio_delta": ratio - 1.0,
            "absolute_ratio_delta": abs(ratio - 1.0),
        }
    elif method in {"categorical_exact", "descriptive_pair"}:
        exact = current_normalized == historical_normalized
        difference = {
            "kind": method,
            "exact_match": exact,
            "relationship": "same" if exact else "different",
        }
    elif method == "categorical_compatibility":
        if definition.compatibility_domain == "role":
            rule = role_compatibility(
                str(current_normalized), str(historical_normalized)
            )
        elif definition.compatibility_domain == "family":
            rule = family_compatibility(
                str(current_normalized), str(historical_normalized)
            )
        else:
            return {
                "state": "unsupported_by_spec",
                "normalized_current_value": current_normalized,
                "normalized_historical_value": historical_normalized,
                "difference": None,
                "tolerance_band_result": {"status": "unavailable"},
                "reason": "Categorical compatibility requires an explicit role or family domain.",
            }
        difference = {
            "kind": method,
            "compatibility_class": rule["compatibility_class"],
            "rule_id": rule["rule_id"],
            "reason": rule["reason"],
            "matrix_location": rule.get("matrix_location"),
            "matrix_version": rule.get("matrix_version"),
        }
    elif method == "ordered_state_distance":
        order = {value.lower(): index for index, value in enumerate(definition.ordered_values)}
        current_key = str(current_normalized).lower()
        historical_key = str(historical_normalized).lower()
        if current_key not in order or historical_key not in order:
            return {
                "state": "unsupported_by_spec",
                "normalized_current_value": current_normalized,
                "normalized_historical_value": historical_normalized,
                "difference": None,
                "tolerance_band_result": {"status": "unavailable"},
                "reason": "One or both ordered states are absent from the versioned order.",
            }
        signed = order[current_key] - order[historical_key]
        difference = {
            "kind": method,
            "current_index": order[current_key],
            "historical_index": order[historical_key],
            "signed_order_distance": signed,
            "absolute_order_distance": abs(signed),
            "exact_match": signed == 0,
        }
    else:
        return {
            "state": "unsupported_by_spec",
            "normalized_current_value": current_normalized,
            "normalized_historical_value": historical_normalized,
            "difference": None,
            "tolerance_band_result": {"status": "unavailable"},
            "reason": f"Unsupported comparison method {method!r}.",
        }
    return {
        "state": "comparable",
        "normalized_current_value": current_normalized,
        "normalized_historical_value": historical_normalized,
        "difference": difference,
        "tolerance_band_result": _tolerance_result(definition, difference),
        "reason": "Compared using the explicit versioned dimension method.",
    }


def _projection(dna: Mapping[str, Any]) -> dict[str, Any]:
    groups = dna.get("groups") if isinstance(dna.get("groups"), Mapping) else {}
    return {
        "groups": deepcopy(dict(groups)),
        "descriptor": endpoint_descriptor(dna),
    }


def _source(projection: Mapping[str, Any]) -> Mapping[str, Any]:
    found, value = _field(projection, "groups.source_identity.values.source")
    return value if found and isinstance(value, Mapping) else {}


def _requirement_state(
    definition: DimensionDefinition,
    current_projection: Mapping[str, Any],
    historical_projection: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    current_source = _source(current_projection)
    historical_source = _source(historical_projection)
    for requirement in definition.requirements:
        if requirement == "same_feed":
            fields = ("provider", "feed_identity")
            if any(
                not str(current_source.get(field) or "").strip()
                or not str(historical_source.get(field) or "").strip()
                for field in fields
            ):
                return (
                    "provenance_incomparable",
                    "Same-feed comparison requires provider and feed identity on both endpoints.",
                )
            if any(current_source.get(field) != historical_source.get(field) for field in fields):
                return (
                    "feed_incomparable",
                    "This raw dimension requires identical provider and feed identity.",
                )
        elif requirement == "same_timeframe":
            current_timeframe = str(current_source.get("timeframe") or "").strip()
            historical_timeframe = str(historical_source.get("timeframe") or "").strip()
            if not current_timeframe or not historical_timeframe:
                return (
                    "provenance_incomparable",
                    "Same-timeframe comparison requires timeframe provenance on both endpoints.",
                )
            if current_timeframe != historical_timeframe:
                return (
                    "provenance_incomparable",
                    "This dimension requires identical source timeframes.",
                )
        elif requirement == "same_volume_scope":
            current_scope = str(current_source.get("volume_scope") or "").strip()
            historical_scope = str(historical_source.get("volume_scope") or "").strip()
            if not current_scope or not historical_scope:
                return (
                    "provenance_incomparable",
                    "Normalized volume comparison requires volume scope on both endpoints.",
                )
            if current_scope != historical_scope:
                return (
                    "provenance_incomparable",
                    "Normalized volume comparison requires identical volume scope.",
                )
        else:
            return (
                "unsupported_by_spec",
                f"Unknown provenance requirement {requirement!r}.",
            )
    return None, None


def _group_name(source_field: str) -> str | None:
    parts = source_field.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "groups" else None


def _dimension_provenance(
    dna: Mapping[str, Any], definition: DimensionDefinition
) -> dict[str, Any]:
    group_name = _group_name(definition.source_field)
    groups = dna.get("groups") if isinstance(dna.get("groups"), Mapping) else {}
    group = groups.get(group_name) if group_name and isinstance(groups.get(group_name), Mapping) else {}
    return {
        "endpoint_dna_hash": dna.get("content_hash"),
        "source_fingerprint_hash": dna.get("source_fingerprint_hash"),
        "source_endpoint_snapshot_id": dna.get("source_endpoint_snapshot_id"),
        "source_endpoint_snapshot_hash": dna.get("source_endpoint_snapshot_hash"),
        "endpoint_cutoff": dna.get("cutoff"),
        "feature_group": group_name,
        "feature_group_status": group.get("status"),
        "feature_group_cutoff": group.get("cutoff"),
        "feature_group_calculation_version": group.get("calculation_version"),
        "source_references": deepcopy(group.get("source_references") or []),
    }


def _missing_state(current_value: Any, historical_value: Any) -> str | None:
    current_missing = current_value is None
    historical_missing = historical_value is None
    if current_missing and historical_missing:
        return "missing_both"
    if current_missing:
        return "missing_current"
    if historical_missing:
        return "missing_historical"
    return None


def _compare_dimension(
    definition: DimensionDefinition,
    current_dna: Mapping[str, Any],
    historical_dna: Mapping[str, Any],
    current_projection: Mapping[str, Any],
    historical_projection: Mapping[str, Any],
    filter_item: Mapping[str, Any],
) -> dict[str, Any]:
    level = int(filter_item.get("comparison_level") or 0)
    base = {
        "dimension_id": definition.dimension_id,
        "name": definition.name,
        "group": definition.group,
        "source_field": definition.source_field,
        "data_type": definition.data_type,
        "comparison_method": definition.comparison_method,
        "normalization_rule": definition.normalization_rule,
        "distance_producing": definition.distance_producing,
        "specification_version": definition.specification_version,
        "current_value": None,
        "historical_value": None,
        "normalized_current_value": None,
        "normalized_historical_value": None,
        "difference": None,
        "tolerance_band_result": {
            "status": "unavailable",
            "metric": definition.tolerance_metric,
            "observed": None,
            "threshold": definition.tolerance,
            "inclusive": True,
        },
        "comparison_level_result": {
            "comparison_level": level,
            "allowed_levels": list(definition.allowed_levels),
            "permitted": level in definition.allowed_levels,
            "reason": (
                "Dimension is explicitly permitted at this Phase 5A.2 level."
                if level in definition.allowed_levels
                else "Dimension is omitted because its specification does not permit this comparison level."
            ),
        },
        "provenance": {
            "current": _dimension_provenance(current_dna, definition),
            "historical": _dimension_provenance(historical_dna, definition),
        },
    }
    if level not in definition.allowed_levels:
        return {
            **base,
            "state": "structurally_inapplicable",
            "reason": base["comparison_level_result"]["reason"],
        }

    requirement_state, requirement_reason = _requirement_state(
        definition, current_projection, historical_projection
    )
    if requirement_state is not None:
        return {
            **base,
            "state": requirement_state,
            "reason": requirement_reason,
        }

    incomparable_tags = set(str(item) for item in filter_item.get("incomparable_groups") or [])
    matched_tags = incomparable_tags.intersection(definition.incomparable_tags)
    if matched_tags:
        current_feed = _source(current_projection).get("feed_identity")
        historical_feed = _source(historical_projection).get("feed_identity")
        state = (
            "feed_incomparable"
            if current_feed and historical_feed and current_feed != historical_feed
            else "provenance_incomparable"
        )
        return {
            **base,
            "state": state,
            "reason": (
                "Phase 5A.2 marked this feature group incomparable: "
                + ", ".join(sorted(matched_tags))
            ),
        }

    current_found, current_value = _field(current_projection, definition.source_field)
    historical_found, historical_value = _field(
        historical_projection, definition.source_field
    )
    current_value = current_value if current_found else None
    historical_value = historical_value if historical_found else None
    base["current_value"] = deepcopy(current_value)
    base["historical_value"] = deepcopy(historical_value)
    missing = _missing_state(current_value, historical_value)
    if missing is not None:
        return {
            **base,
            "state": missing,
            "reason": definition.missing_data_behavior,
        }

    compared = compare_dimension_values(definition, current_value, historical_value)
    return {**base, **compared}


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "experience_case_id": candidate.get("experience_case_id"),
        "market_episode_id": candidate.get("market_episode_id"),
        "case_version": candidate.get("case_version"),
    }


def _endpoint_reference_status(
    candidate: Mapping[str, Any], dna: Mapping[str, Any]
) -> dict[str, Any]:
    references = {
        "endpoint_dna_hash": dna.get("content_hash"),
        "source_fingerprint_hash": dna.get("source_fingerprint_hash"),
        "source_endpoint_snapshot_id": dna.get("source_endpoint_snapshot_id"),
        "source_endpoint_snapshot_hash": dna.get("source_endpoint_snapshot_hash"),
    }
    missing = [key for key, value in references.items() if not str(value or "").strip()]
    stored_integrity = candidate.get("source_reference_integrity")
    stored_valid = (
        stored_integrity.get("valid")
        if isinstance(stored_integrity, Mapping)
        else None
    )
    return {
        "valid": not missing and stored_valid is not False,
        "missing_references": missing,
        "stored_reference_integrity": deepcopy(stored_integrity),
        "references": references,
        "policy": "References identify immutable Phase 3 fingerprint and Phase 4 endpoint snapshot sources.",
    }


def _filter_reference(filter_item: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "phase5a2_filter_schema_version": COMPARISON_SCHEMA_VERSION,
        "experience_case_id": filter_item.get("experience_case_id"),
        "market_episode_id": filter_item.get("market_episode_id"),
        "eligible": filter_item.get("eligible"),
        "excluded": filter_item.get("excluded"),
        "comparison_level": filter_item.get("comparison_level"),
        "compatibility_class": filter_item.get("compatibility_class"),
        "rule": filter_item.get("rule"),
        "matrix_location": filter_item.get("matrix_location"),
        "compatibility_rule": deepcopy(filter_item.get("compatibility_rule") or {}),
        "incomparable_groups": sorted(
            str(item) for item in filter_item.get("incomparable_groups") or []
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _dimension_lists(dimensions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    comparable = [
        str(item["dimension_id"])
        for item in dimensions
        if item.get("state") == "comparable"
    ]
    missing = [
        {
            "dimension_id": item.get("dimension_id"),
            "state": item.get("state"),
            "reason": item.get("reason"),
        }
        for item in dimensions
        if item.get("state") in MISSING_STATES
    ]
    incomparable = [
        {
            "dimension_id": item.get("dimension_id"),
            "state": item.get("state"),
            "reason": item.get("reason"),
        }
        for item in dimensions
        if item.get("state") in INCOMPARABLE_STATES
    ]
    omitted = [
        {
            "dimension_id": item.get("dimension_id"),
            "state": item.get("state"),
            "reason": item.get("reason"),
        }
        for item in dimensions
        if item.get("state") in OMITTED_STATES
    ]
    state_counts = {
        state: sum(item.get("state") == state for item in dimensions)
        for state in DIMENSION_STATES
    }
    group_state_counts: dict[str, dict[str, int]] = {}
    for item in dimensions:
        group = str(item.get("group") or "unknown")
        state = str(item.get("state") or "unsupported_by_spec")
        group_state_counts.setdefault(group, {key: 0 for key in DIMENSION_STATES})
        group_state_counts[group][state] += 1
    return {
        "comparable_dimensions": comparable,
        "missing_dimensions": missing,
        "incomparable_dimensions": incomparable,
        "omitted_dimensions": omitted,
        "dimension_state_counts": state_counts,
        "group_state_counts": group_state_counts,
    }


def compare_endpoint_pair(
    current_candidate: Mapping[str, Any],
    historical_candidate: Mapping[str, Any],
    filter_item: Mapping[str, Any],
    *,
    specification_version: str = COMPARISON_SPECIFICATION_VERSION,
) -> dict[str, Any]:
    """Compare one Phase 5A.2-eligible historical endpoint with the current endpoint."""
    specification = comparison_specification(specification_version)
    if filter_item.get("eligible") is not True or filter_item.get("excluded") is True:
        raise ValueError("Phase 5A.3 may compare only a Phase 5A.2-eligible case.")
    level = filter_item.get("comparison_level")
    if level not in {1, 2, 3, 4}:
        raise ValueError("The Phase 5A.2 filter item has no valid progressive level.")
    current_dna = current_candidate.get("endpoint_dna")
    historical_dna = historical_candidate.get("endpoint_dna")
    if not isinstance(current_dna, Mapping) or not isinstance(historical_dna, Mapping):
        raise ValueError("Both candidates require endpoint DNA.")
    current_validation = validate_endpoint_dna(current_dna)
    historical_validation = validate_endpoint_dna(historical_dna)
    if not current_validation["valid"] or not historical_validation["valid"]:
        raise ValueError("Both endpoint DNA records must pass Phase 5A.2 validation.")

    current_projection = _projection(current_dna)
    historical_projection = _projection(historical_dna)
    dimensions = [
        _compare_dimension(
            definition,
            current_dna,
            historical_dna,
            current_projection,
            historical_projection,
            filter_item,
        )
        for definition in DIMENSION_DEFINITIONS
    ]
    current_reference_status = _endpoint_reference_status(
        current_candidate, current_dna
    )
    historical_reference_status = _endpoint_reference_status(
        historical_candidate, historical_dna
    )
    warnings = [deepcopy(item) for item in filter_item.get("soft_penalties") or []]
    if not current_reference_status["valid"]:
        warnings.append(
            {
                "code": "current_immutable_reference_incomplete",
                "reason": "Current endpoint immutable source references are incomplete or invalid.",
                "effect": "provenance_warning_no_score",
            }
        )
    if not historical_reference_status["valid"]:
        warnings.append(
            {
                "code": "historical_immutable_reference_incomplete",
                "reason": "Historical endpoint immutable source references are incomplete or invalid.",
                "effect": "provenance_warning_no_score",
            }
        )
    payload: dict[str, Any] = {
        "schema_version": ANALOGUE_COMPARISON_SCHEMA_VERSION,
        "calculation_version": ANALOGUE_COMPARISON_CALCULATION_VERSION,
        "comparison_specification_version": specification_version,
        "comparison_specification_hash": specification["content_hash"],
        "current_endpoint_identifier": _candidate_identity(current_candidate),
        "historical_experience_identifier": _candidate_identity(
            historical_candidate
        ),
        "filter_result_reference": _filter_reference(filter_item),
        "comparison_level_result": {
            "level": int(level),
            "reason": filter_item.get("comparison_reason") or filter_item.get("reason"),
            "compatibility_class": filter_item.get("compatibility_class"),
            "comparison_scope": filter_item.get("comparison_scope"),
        },
        "dimensions": dimensions,
        **_dimension_lists(dimensions),
        "provenance": {
            "current": {
                "source": deepcopy(current_projection["descriptor"].get("source") or {}),
                **current_reference_status,
            },
            "historical": {
                "source": deepcopy(historical_projection["descriptor"].get("source") or {}),
                **historical_reference_status,
            },
        },
        "source_hashes": {
            "current_endpoint_dna": current_dna.get("content_hash"),
            "current_fingerprint": current_dna.get("source_fingerprint_hash"),
            "current_endpoint_snapshot": current_dna.get("source_endpoint_snapshot_hash"),
            "historical_endpoint_dna": historical_dna.get("content_hash"),
            "historical_fingerprint": historical_dna.get("source_fingerprint_hash"),
            "historical_endpoint_snapshot": historical_dna.get("source_endpoint_snapshot_hash"),
        },
        "cutoff_validation": {
            "current": {
                "cutoff": current_dna.get("cutoff"),
                "valid": current_validation["valid"],
                "reasons": deepcopy(current_validation["reasons"]),
            },
            "historical": {
                "cutoff": historical_dna.get("cutoff"),
                "valid": historical_validation["valid"],
                "reasons": deepcopy(historical_validation["reasons"]),
            },
        },
        "warnings": warnings,
        "distance_summary_policy": (
            "No grouped or overall distance and no cross-case rank are calculated."
        ),
        "input_policy": (
            "Endpoint DNA only. Confirmation DNA and reviewed outcome DNA were not read."
        ),
    }
    identity_hash = _hash_payload(payload)
    payload["comparison_id"] = f"analogue_comparison_{identity_hash[:32]}"
    payload["content_hash"] = _hash_payload(payload)
    return payload


def compare_filtered_experiences(
    current_candidate: Mapping[str, Any],
    historical_cases: Iterable[Mapping[str, Any]],
    filter_result: Mapping[str, Any],
    *,
    candidate_case_ids: Iterable[str] | None = None,
    maximum_comparison_level: int = 4,
    specification_version: str = COMPARISON_SPECIFICATION_VERSION,
) -> dict[str, Any]:
    """Compare only the eligible portion of an existing Phase 5A.2 result."""
    specification = comparison_specification(specification_version)
    if maximum_comparison_level not in {1, 2, 3, 4}:
        raise ValueError("maximum_comparison_level must be 1, 2, 3, or 4.")
    current_id = str(current_candidate.get("experience_case_id") or "")
    filtered_current = str(
        (filter_result.get("current_candidate") or {}).get("experience_case_id")
        if isinstance(filter_result.get("current_candidate"), Mapping)
        else ""
    )
    if not current_id or current_id != filtered_current:
        raise ValueError("The Phase 5A.2 filter result belongs to a different current case.")
    selected = (
        {str(item) for item in candidate_case_ids}
        if candidate_case_ids is not None
        else None
    )
    historical = {
        str(item.get("experience_case_id") or ""): deepcopy(dict(item))
        for item in historical_cases
    }
    eligible_items = [
        deepcopy(dict(item))
        for item in filter_result.get("eligible") or []
        if isinstance(item, Mapping)
        and (selected is None or str(item.get("experience_case_id") or "") in selected)
    ]
    excluded_items = [
        deepcopy(dict(item))
        for item in filter_result.get("excluded") or []
        if isinstance(item, Mapping)
        and (selected is None or str(item.get("experience_case_id") or "") in selected)
    ]
    known_ids = set(historical)
    requested_unknown = sorted((selected or set()).difference(known_ids))
    comparisons: list[dict[str, Any]] = []
    omitted_by_level: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for case_id in requested_unknown:
        warnings.append(
            {
                "code": "unknown_candidate_case",
                "experience_case_id": case_id,
                "reason": "The requested candidate does not exist in the supplied historical set.",
            }
        )
    for filter_item in eligible_items:
        case_id = str(filter_item.get("experience_case_id") or "")
        level = int(filter_item.get("comparison_level") or 0)
        if level > maximum_comparison_level:
            omitted_by_level.append(
                {
                    "experience_case_id": case_id,
                    "comparison_level": level,
                    "rule": "REQUESTED-MAXIMUM-LEVEL",
                    "reason": (
                        f"Candidate is Phase 5A.2 eligible at Level {level}, above the "
                        f"requested maximum Level {maximum_comparison_level}."
                    ),
                }
            )
            continue
        candidate = historical.get(case_id)
        if candidate is None:
            warnings.append(
                {
                    "code": "eligible_candidate_payload_missing",
                    "experience_case_id": case_id,
                    "reason": "Phase 5A.2 marked the case eligible but its endpoint payload is unavailable.",
                }
            )
            continue
        comparisons.append(
            compare_endpoint_pair(
                current_candidate,
                candidate,
                filter_item,
                specification_version=specification_version,
            )
        )
    payload: dict[str, Any] = {
        "schema_version": ANALOGUE_COMPARISON_SCHEMA_VERSION,
        "calculation_version": ANALOGUE_COMPARISON_CALCULATION_VERSION,
        "comparison_specification_version": specification_version,
        "comparison_specification_hash": specification["content_hash"],
        "current_endpoint_identifier": _candidate_identity(current_candidate),
        "phase5a2_filter_reference": {
            "schema_version": filter_result.get("schema_version"),
            "calculation_version": filter_result.get("calculation_version"),
            "compatibility_matrix_version": filter_result.get(
                "compatibility_matrix_version"
            ),
            "content_hash": _hash_payload(filter_result),
        },
        "requested_candidate_case_ids": sorted(selected) if selected is not None else None,
        "maximum_comparison_level": maximum_comparison_level,
        "comparisons": comparisons,
        "phase5a2_excluded_cases": excluded_items,
        "eligible_but_omitted_by_requested_level": omitted_by_level,
        "warnings": warnings,
        "counts": {
            "phase5a2_eligible_selected": len(eligible_items),
            "compared": len(comparisons),
            "phase5a2_excluded_selected": len(excluded_items),
            "eligible_but_omitted_by_requested_level": len(omitted_by_level),
            "unknown_requested_candidates": len(requested_unknown),
        },
        "status": (
            "completed"
            if comparisons
            else "unavailable_no_eligible_candidates"
        ),
        "ordering_policy": (
            "Comparisons preserve Phase 5A.2 stable identity order; no relevance rank exists."
        ),
        "input_policy": (
            "Endpoint DNA only. Confirmation DNA and reviewed outcome DNA were not read."
        ),
        "deferred": (
            "Cross-case ranking, outcome display/aggregation, evaluation, and canonical memory remain Phase 5A.4 work."
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _display(value: Any, limit: int = 70) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        text = f"{value:.8g}"
    elif isinstance(value, (str, int, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_analogue_comparisons(
    result: Mapping[str, Any],
    *,
    explain: bool = False,
    show_provenance: bool = False,
    show_hashes: bool = False,
) -> str:
    """Render a deterministic human-readable view without changing comparison data."""
    current = result.get("current_endpoint_identifier") or {}
    counts = result.get("counts") or {}
    lines = [
        "# Experience Analogue Comparison",
        "",
        f"Current endpoint: {current.get('experience_case_id') or 'unknown'}",
        f"Specification: {result.get('comparison_specification_version')}",
        f"Status: {result.get('status')}",
        f"Compared cases: {counts.get('compared', 0)}",
        "Ordering: stable identity order only; no best-analogue rank.",
        "Inputs: endpoint DNA only; no confirmation or reviewed outcome DNA.",
    ]
    for comparison in result.get("comparisons") or []:
        historical = comparison.get("historical_experience_identifier") or {}
        level = comparison.get("comparison_level_result") or {}
        state_counts = comparison.get("dimension_state_counts") or {}
        lines.extend(
            [
                "",
                f"## {historical.get('experience_case_id') or 'unknown'}",
                "",
                f"Level: {level.get('level')} ({level.get('compatibility_class')})",
                f"Comparable dimensions: {state_counts.get('comparable', 0)}",
                "Missing dimensions: "
                + str(sum(state_counts.get(state, 0) for state in MISSING_STATES)),
                "Incomparable dimensions: "
                + str(sum(state_counts.get(state, 0) for state in INCOMPARABLE_STATES)),
                "Omitted/unsupported dimensions: "
                + str(sum(state_counts.get(state, 0) for state in OMITTED_STATES)),
            ]
        )
        if explain:
            lines.extend(
                [
                    "",
                    "| Dimension | State | Current | Historical | Difference | Tolerance |",
                    "|---|---|---:|---:|---|---|",
                ]
            )
            for dimension in comparison.get("dimensions") or []:
                tolerance = dimension.get("tolerance_band_result") or {}
                lines.append(
                    "| "
                    + " | ".join(
                        (
                            str(dimension.get("dimension_id")),
                            str(dimension.get("state")),
                            _display(dimension.get("current_value")),
                            _display(dimension.get("historical_value")),
                            _display(dimension.get("difference")),
                            str(tolerance.get("status") or "unavailable"),
                        )
                    )
                    + " |"
                )
        if show_provenance:
            lines.extend(
                [
                    "",
                    "Provenance:",
                    "",
                    "```json",
                    json.dumps(
                        comparison.get("provenance"),
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                ]
            )
        if show_hashes:
            lines.extend(
                [
                    "",
                    f"Comparison hash: {comparison.get('content_hash')}",
                    "Source hashes: "
                    + json.dumps(
                        comparison.get("source_hashes"),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                ]
            )
    if not result.get("comparisons"):
        lines.extend(
            [
                "",
                "No Phase 5A.2-eligible historical endpoint is available at the requested level.",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
