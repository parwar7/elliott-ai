"""Deterministic mechanism-level exposure mapping.

The engine maps validated evidence into specific exposure mechanisms. Broad
research categories are tracked separately through coverage declarations.
Ambiguous evidence remains unmapped with a structured warning. No model,
provider, live research, Elliott logic, or prediction is used here.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .market_scenario import (
    EXPOSURE_CATEGORY_COMPATIBILITY,
    EXPOSURE_TYPE_CATEGORY,
    QUALITATIVE_CONFIDENCE_VALUES,
    EvidenceImplication,
    EvidenceItem,
    EvidenceStatus,
    ExposureCategory,
    ExposureCategoryCoverage,
    ExposureCoverageState,
    ExposureItem,
    ExposureMappingWarning,
    ExposureType,
)


EXPOSURE_MAPPING_VERSION = "market-scenario-exposure-map-2.0.0"
EXPOSURE_CONFIDENCE_VERSION = "market-scenario-exposure-confidence-1.0.0"

_CONFIDENCE_STRENGTH = {
    "unavailable": 0,
    "insufficient_evidence": 0,
    "low": 1,
    "moderate": 2,
    "high": 3,
}
_STRENGTH_CONFIDENCE = {
    0: "insufficient_evidence",
    1: "low",
    2: "moderate",
    3: "high",
}
_STATUS_CONFIDENCE_CAP = {
    EvidenceStatus.CONFIRMED: 3,
    EvidenceStatus.SCHEDULED: 3,
    EvidenceStatus.INTERPRETATION: 2,
    EvidenceStatus.HYPOTHETICAL: 1,
    EvidenceStatus.STALE: 1,
    EvidenceStatus.UNKNOWN: 0,
    EvidenceStatus.UNAVAILABLE: 0,
}

# These aliases are explicit policy. Broad categories such as "company" do not
# appear here because they are research lanes, not mechanisms.
EVIDENCE_CATEGORY_MECHANISMS: Mapping[str, tuple[ExposureType, ...]] = (
    MappingProxyType(
        {
            "revenue_growth": (ExposureType.REVENUE_GROWTH_RISK,),
            "revenue_visibility": (ExposureType.REVENUE_VISIBILITY,),
            "earnings": (ExposureType.EARNINGS_RISK,),
            "guidance": (ExposureType.GUIDANCE_RISK,),
            "margin": (ExposureType.MARGIN_PRESSURE,),
            "gross_margin": (ExposureType.MARGIN_PRESSURE,),
            "operating_margin": (ExposureType.MARGIN_PRESSURE,),
            "cash_burn": (ExposureType.CASH_BURN_RISK,),
            "company_liquidity": (ExposureType.LIQUIDITY_RISK,),
            "financing": (ExposureType.FINANCING_RISK,),
            "dilution": (ExposureType.DILUTION_RISK,),
            "debt": (ExposureType.DEBT_RISK,),
            "execution": (ExposureType.EXECUTION_RISK,),
            "operations": (ExposureType.OPERATIONAL_RISK,),
            "development_delay": (ExposureType.DEVELOPMENT_DELAY_RISK,),
            "company_supply_chain": (ExposureType.SUPPLY_CHAIN_RISK,),
            "customer_concentration": (
                ExposureType.CUSTOMER_CONCENTRATION_RISK,
            ),
            "contract": (ExposureType.CONTRACT_RISK,),
            "company_regulation": (ExposureType.REGULATORY_RISK,),
            "litigation": (ExposureType.LITIGATION_RISK,),
            "management": (ExposureType.MANAGEMENT_RISK,),
            "acquisition_integration": (
                ExposureType.ACQUISITION_INTEGRATION_RISK,
            ),
            "sector_demand": (ExposureType.SECTOR_DEMAND_RISK,),
            "sector_valuation": (ExposureType.SECTOR_VALUATION_RISK,),
            "competition": (ExposureType.COMPETITIVE_PRESSURE,),
            "pricing_pressure": (ExposureType.PRICING_PRESSURE,),
            "market_share": (ExposureType.MARKET_SHARE_RISK,),
            "competitor_execution": (
                ExposureType.COMPETITOR_EXECUTION_RISK,
            ),
            "sector_regulation": (ExposureType.SECTOR_REGULATORY_RISK,),
            "government_spending": (
                ExposureType.GOVERNMENT_SPENDING_EXPOSURE,
            ),
            "sector_supply_chain": (
                ExposureType.SECTOR_SUPPLY_CHAIN_RISK,
            ),
            "sector_sentiment": (ExposureType.SECTOR_SENTIMENT_RISK,),
            "interest_rates": (ExposureType.INTEREST_RATE_SENSITIVITY,),
            "inflation": (ExposureType.INFLATION_SENSITIVITY,),
            "recession": (ExposureType.RECESSION_SENSITIVITY,),
            "market_liquidity": (ExposureType.LIQUIDITY_CONDITIONS,),
            "credit_conditions": (ExposureType.CREDIT_CONDITIONS,),
            "risk_appetite": (ExposureType.EQUITY_RISK_APPETITE,),
            "small_cap": (ExposureType.SMALL_CAP_SENSITIVITY,),
            "growth_stock": (ExposureType.GROWTH_STOCK_SENSITIVITY,),
            "currency": (ExposureType.CURRENCY_EXPOSURE,),
            "commodity": (ExposureType.COMMODITY_EXPOSURE,),
            "geopolitics": (ExposureType.GEOPOLITICAL_EXPOSURE,),
            "valuation_compression": (ExposureType.VALUATION_COMPRESSION,),
            "valuation_expansion": (ExposureType.VALUATION_EXPANSION,),
            "growth_expectations": (ExposureType.GROWTH_EXPECTATION_RISK,),
            "analyst_revisions": (ExposureType.ANALYST_REVISION_RISK,),
            "institutional_positioning": (
                ExposureType.INSTITUTIONAL_POSITIONING,
            ),
            "insider_positioning": (ExposureType.INSIDER_POSITIONING,),
            "short_interest": (ExposureType.SHORT_INTEREST_RISK,),
            "options_positioning": (ExposureType.OPTIONS_POSITIONING,),
            "crowded_trade": (ExposureType.CROWDED_TRADE_RISK,),
            "ownership_concentration": (
                ExposureType.OWNERSHIP_CONCENTRATION_RISK,
            ),
            "momentum_unwind": (ExposureType.MOMENTUM_UNWIND_RISK,),
            "earnings_event": (ExposureType.EARNINGS_EVENT_RISK,),
            "product_milestone": (ExposureType.PRODUCT_MILESTONE_RISK,),
            "launch_event": (ExposureType.LAUNCH_EVENT_RISK,),
            "regulatory_event": (ExposureType.REGULATORY_EVENT_RISK,),
            "contract_award": (ExposureType.CONTRACT_AWARD_RISK,),
            "financing_event": (ExposureType.FINANCING_EVENT_RISK,),
            "central_bank_event": (ExposureType.CENTRAL_BANK_EVENT_RISK,),
            "economic_release": (ExposureType.ECONOMIC_RELEASE_RISK,),
            "index_event": (ExposureType.INDEX_EVENT_RISK,),
            "sell_the_news": (ExposureType.SELL_THE_NEWS_RISK,),
            "pre_event_uncertainty": (ExposureType.PRE_EVENT_UNCERTAINTY,),
        }
    )
)

# Claim rules require explicit mechanism phrases. They do not infer broad causal
# stories or convert generic sentiment into a specific mechanism.
_CLAIM_MECHANISM_RULES: tuple[tuple[re.Pattern[str], ExposureType], ...] = (
    (re.compile(r"\brevenue growth\b", re.I), ExposureType.REVENUE_GROWTH_RISK),
    (re.compile(r"\brevenue visibility\b", re.I), ExposureType.REVENUE_VISIBILITY),
    (re.compile(r"\bearnings risk\b", re.I), ExposureType.EARNINGS_RISK),
    (re.compile(r"\bguidance risk\b", re.I), ExposureType.GUIDANCE_RISK),
    (re.compile(r"\bmargin pressure\b", re.I), ExposureType.MARGIN_PRESSURE),
    (re.compile(r"\bcash burn\b", re.I), ExposureType.CASH_BURN_RISK),
    (re.compile(r"\bfinancing risk\b", re.I), ExposureType.FINANCING_RISK),
    (re.compile(r"\bdilution risk\b", re.I), ExposureType.DILUTION_RISK),
    (re.compile(r"\bdebt risk\b", re.I), ExposureType.DEBT_RISK),
    (re.compile(r"\bexecution risk\b", re.I), ExposureType.EXECUTION_RISK),
    (
        re.compile(r"\binterest[- ]rate sensitivity\b", re.I),
        ExposureType.INTEREST_RATE_SENSITIVITY,
    ),
    (
        re.compile(r"\bmarket liquidity\b", re.I),
        ExposureType.LIQUIDITY_CONDITIONS,
    ),
    (
        re.compile(r"\bvaluation compression\b", re.I),
        ExposureType.VALUATION_COMPRESSION,
    ),
    (
        re.compile(r"\bvaluation expansion\b", re.I),
        ExposureType.VALUATION_EXPANSION,
    ),
    (re.compile(r"\bshort interest\b", re.I), ExposureType.SHORT_INTEREST_RISK),
    (
        re.compile(r"\bearnings event\b", re.I),
        ExposureType.EARNINGS_EVENT_RISK,
    ),
    (
        re.compile(r"\bcentral bank event\b", re.I),
        ExposureType.CENTRAL_BANK_EVENT_RISK,
    ),
    (
        re.compile(r"\beconomic release\b", re.I),
        ExposureType.ECONOMIC_RELEASE_RISK,
    ),
    (
        re.compile(r"\bpre[- ]event uncertainty\b", re.I),
        ExposureType.PRE_EVENT_UNCERTAINTY,
    ),
)


@dataclass(frozen=True, slots=True)
class ExposureMappingResult:
    mapping_version: str
    exposures: tuple[ExposureItem, ...]
    coverage: tuple[ExposureCategoryCoverage, ...]
    unmapped_evidence_ids: tuple[str, ...]
    warnings: tuple[ExposureMappingWarning, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.mapping_version, str) or not self.mapping_version:
            raise ValueError("mapping_version must be a non-empty string.")
        object.__setattr__(self, "exposures", tuple(self.exposures))
        object.__setattr__(self, "coverage", tuple(self.coverage))
        object.__setattr__(
            self,
            "unmapped_evidence_ids",
            tuple(self.unmapped_evidence_ids),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not all(isinstance(item, ExposureItem) for item in self.exposures):
            raise TypeError("exposures must contain only ExposureItem values.")
        if not all(
            isinstance(item, ExposureCategoryCoverage) for item in self.coverage
        ):
            raise TypeError(
                "coverage must contain only ExposureCategoryCoverage values."
            )
        if not all(
            isinstance(item, ExposureMappingWarning) for item in self.warnings
        ):
            raise TypeError(
                "warnings must contain only ExposureMappingWarning values."
            )
        if not all(
            isinstance(item, str) for item in self.unmapped_evidence_ids
        ):
            raise TypeError("unmapped_evidence_ids must contain only strings.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_version": self.mapping_version,
            "exposures": [item.to_dict() for item in self.exposures],
            "coverage": [item.to_dict() for item in self.coverage],
            "unmapped_evidence_ids": list(self.unmapped_evidence_ids),
            "warnings": [item.to_dict() for item in self.warnings],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExposureMappingResult":
        if not isinstance(value, Mapping):
            raise TypeError("ExposureMappingResult.from_dict requires a mapping.")
        allowed = {
            "mapping_version",
            "exposures",
            "coverage",
            "unmapped_evidence_ids",
            "warnings",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                "ExposureMappingResult contains unknown fields: "
                + ", ".join(unknown)
                + "."
            )
        return cls(
            mapping_version=str(value["mapping_version"]),
            exposures=tuple(
                ExposureItem.from_dict(item) for item in value.get("exposures", ())
            ),
            coverage=tuple(
                ExposureCategoryCoverage.from_dict(item)
                for item in value.get("coverage", ())
            ),
            unmapped_evidence_ids=tuple(
                value.get("unmapped_evidence_ids", ())
            ),
            warnings=tuple(
                ExposureMappingWarning.from_dict(item)
                for item in value.get("warnings", ())
            ),
        )


def _normalize(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip(
        "_"
    )


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _type_order(exposure_type: ExposureType) -> int:
    return tuple(ExposureType).index(exposure_type)


def _category_parts(
    evidence: EvidenceItem,
) -> tuple[set[ExposureCategory], set[ExposureType], tuple[str, ...]]:
    categories: set[ExposureCategory] = set()
    mechanisms: set[ExposureType] = set()
    unsupported: list[str] = []
    parts = [
        part.strip()
        for part in re.split(r"[,;|/+]", evidence.category)
        if part.strip()
    ]
    for raw_part in parts or [evidence.category]:
        normalized = _normalize(raw_part)
        try:
            category = ExposureCategory(normalized)
        except ValueError:
            category = None
        if category is not None:
            categories.add(category)
            continue
        try:
            exposure_type = ExposureType(normalized)
        except ValueError:
            exposure_type = None
        if exposure_type is not None:
            mechanisms.add(exposure_type)
            categories.add(EXPOSURE_TYPE_CATEGORY[exposure_type])
            continue
        mapped = EVIDENCE_CATEGORY_MECHANISMS.get(normalized)
        if mapped:
            mechanisms.update(mapped)
            categories.update(EXPOSURE_TYPE_CATEGORY[item] for item in mapped)
        else:
            unsupported.append(raw_part)
    return categories, mechanisms, tuple(unsupported)


def _explicit_type_values(
    evidence_id: str,
    value: Mapping[str, Sequence[ExposureType | str]] | None,
) -> tuple[ExposureType | str, ...]:
    if not value or evidence_id not in value:
        return ()
    raw = value[evidence_id]
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError(
            f"exposure_types_by_evidence[{evidence_id!r}] must be a sequence."
        )
    return tuple(raw)


def _annotation_map(
    value: Mapping[ExposureType | str, Sequence[str]] | None,
    *,
    field_name: str,
) -> dict[ExposureType, tuple[str, ...]]:
    result = {item: () for item in ExposureType}
    for raw_key, raw_values in (value or {}).items():
        try:
            exposure_type = (
                raw_key if isinstance(raw_key, ExposureType) else ExposureType(raw_key)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{field_name} contains unsupported exposure type {raw_key!r}."
            ) from exc
        if isinstance(raw_values, (str, bytes)) or not isinstance(
            raw_values, Sequence
        ):
            raise TypeError(f"{field_name}[{exposure_type.value}] must be a sequence.")
        if not all(isinstance(item, str) for item in raw_values):
            raise TypeError(
                f"{field_name}[{exposure_type.value}] must contain strings."
            )
        result[exposure_type] = _ordered_unique(
            tuple(item.strip() for item in raw_values if item.strip())
        )
    return result


def _coverage_map(
    value: Mapping[
        ExposureCategory | str, ExposureCoverageState | str
    ]
    | None,
) -> dict[ExposureCategory, ExposureCoverageState]:
    result: dict[ExposureCategory, ExposureCoverageState] = {}
    for raw_category, raw_state in (value or {}).items():
        try:
            category = (
                raw_category
                if isinstance(raw_category, ExposureCategory)
                else ExposureCategory(raw_category)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"coverage_by_category contains invalid category {raw_category!r}."
            ) from exc
        try:
            state = (
                raw_state
                if isinstance(raw_state, ExposureCoverageState)
                else ExposureCoverageState(raw_state)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"coverage_by_category[{category.value!r}] has invalid state "
                f"{raw_state!r}."
            ) from exc
        result[category] = state
    return result


def _evidence_strength(evidence: EvidenceItem) -> int:
    declared = _CONFIDENCE_STRENGTH[evidence.confidence]
    return min(declared, _STATUS_CONFIDENCE_CAP[evidence.status])


def aggregate_exposure_confidence(
    evidence_items: Sequence[EvidenceItem],
) -> str:
    """Aggregate qualitative evidence strength without probabilities or weights."""
    items = tuple(evidence_items)
    if not items:
        return "unavailable"
    if not all(isinstance(item, EvidenceItem) for item in items):
        raise TypeError("evidence_items must contain only EvidenceItem values.")
    invalid = sorted(
        {
            item.confidence
            for item in items
            if item.confidence not in QUALITATIVE_CONFIDENCE_VALUES
        }
    )
    if invalid:
        raise ValueError(
            "Evidence confidence uses unsupported values: " + ", ".join(invalid)
        )

    supporting_strength = max(
        (
            _evidence_strength(item)
            for item in items
            if item.implication is EvidenceImplication.SUPPORTIVE
        ),
        default=0,
    )
    contradicting_strength = max(
        (
            _evidence_strength(item)
            for item in items
            if item.implication is EvidenceImplication.CONTRADICTORY
        ),
        default=0,
    )
    if supporting_strength == 0 and contradicting_strength == 0:
        return (
            "unavailable"
            if all(item.status is EvidenceStatus.UNAVAILABLE for item in items)
            else "insufficient_evidence"
        )
    if supporting_strength and contradicting_strength:
        if supporting_strength == contradicting_strength:
            return "low"
        return (
            "low"
            if max(supporting_strength, contradicting_strength) == 1
            else "moderate"
        )
    return _STRENGTH_CONFIDENCE[
        max(supporting_strength, contradicting_strength)
    ]


def _aggregate_direction(items: Sequence[EvidenceItem]) -> str:
    supportive = any(
        item.implication is EvidenceImplication.SUPPORTIVE for item in items
    )
    contradictory = any(
        item.implication is EvidenceImplication.CONTRADICTORY for item in items
    )
    if supportive and contradictory:
        return "mixed"
    if supportive:
        return "positive"
    if contradictory:
        return "negative"
    if any(item.implication is EvidenceImplication.NEUTRAL for item in items):
        return "neutral"
    if items and all(
        item.implication is EvidenceImplication.INCOMPARABLE for item in items
    ):
        return "incomparable"
    return "unknown"


def _aggregate_status(items: Sequence[EvidenceItem]) -> EvidenceStatus:
    if not items:
        return EvidenceStatus.UNAVAILABLE
    statuses = {item.status for item in items}
    if len(items) == 1:
        return items[0].status
    if statuses == {EvidenceStatus.UNAVAILABLE}:
        return EvidenceStatus.UNAVAILABLE
    if statuses <= {EvidenceStatus.UNKNOWN, EvidenceStatus.UNAVAILABLE}:
        return EvidenceStatus.UNKNOWN
    if statuses == {EvidenceStatus.STALE}:
        return EvidenceStatus.STALE
    return EvidenceStatus.INTERPRETATION


def _exposure_limitations(items: Sequence[EvidenceItem]) -> tuple[str, ...]:
    limitations: list[str] = []
    if any(item.status is EvidenceStatus.HYPOTHETICAL for item in items):
        limitations.append(
            "Hypothetical evidence is retained as an assumption, not confirmed fact."
        )
    if any(item.status is EvidenceStatus.STALE for item in items):
        limitations.append("Stale evidence cannot carry unqualified current confidence.")
    if any(
        item.implication is EvidenceImplication.INCOMPARABLE for item in items
    ):
        limitations.append("Incomparable evidence does not increase confidence.")
    if any(
        item.implication is EvidenceImplication.SUPPORTIVE for item in items
    ) and any(
        item.implication is EvidenceImplication.CONTRADICTORY for item in items
    ):
        limitations.append("Supporting and contradicting evidence are both retained.")
    return _ordered_unique(limitations)


def _build_exposure(
    category: ExposureCategory,
    exposure_type: ExposureType,
    evidence_items: Sequence[EvidenceItem],
    *,
    explicit_assumptions: Sequence[str],
    related_technical_outcomes: Sequence[str],
) -> ExposureItem:
    items = tuple(sorted(evidence_items, key=lambda item: item.evidence_id))
    supporting_ids = _ordered_unique(
        tuple(
            item.evidence_id
            for item in items
            if item.implication is EvidenceImplication.SUPPORTIVE
        )
    )
    contradicting_ids = _ordered_unique(
        tuple(
            item.evidence_id
            for item in items
            if item.implication is EvidenceImplication.CONTRADICTORY
        )
    )
    assumptions = _ordered_unique(
        tuple(explicit_assumptions)
        + tuple(
            item.claim
            for item in items
            if item.status is EvidenceStatus.HYPOTHETICAL
        )
    )
    return ExposureItem(
        exposure_id=f"exposure-{category.value}-{exposure_type.value}",
        category=category,
        exposure_type=exposure_type,
        summary=(
            f"{exposure_type.value} aggregates {len(items)} validated evidence "
            f"item(s): {len(supporting_ids)} supportive and "
            f"{len(contradicting_ids)} contradictory."
        ),
        materiality="unknown",
        direction=_aggregate_direction(items),
        possible_magnitude=None,
        time_horizon=None,
        status=_aggregate_status(items),
        supporting_evidence_ids=supporting_ids,
        contradicting_evidence_ids=contradicting_ids,
        related_technical_outcomes=_ordered_unique(
            tuple(related_technical_outcomes)
        ),
        confidence=aggregate_exposure_confidence(items),
        assumptions=assumptions,
        limitations=_exposure_limitations(items),
    )


def map_evidence_to_exposures(
    evidence_items: Sequence[EvidenceItem],
    *,
    exposure_types_by_evidence: Mapping[
        str, Sequence[ExposureType | str]
    ]
    | None = None,
    assumptions_by_exposure: Mapping[
        ExposureType | str, Sequence[str]
    ]
    | None = None,
    related_technical_outcomes_by_exposure: Mapping[
        ExposureType | str, Sequence[str]
    ]
    | None = None,
    coverage_by_category: Mapping[
        ExposureCategory | str, ExposureCoverageState | str
    ]
    | None = None,
) -> ExposureMappingResult:
    """Map evidence to specific mechanisms and preserve ambiguous evidence."""
    if isinstance(evidence_items, (str, bytes)) or not isinstance(
        evidence_items, Sequence
    ):
        raise TypeError("evidence_items must be a sequence of EvidenceItem values.")
    items = tuple(evidence_items)
    if not all(isinstance(item, EvidenceItem) for item in items):
        raise TypeError("evidence_items must contain only EvidenceItem values.")
    duplicate_ids = {
        evidence_id
        for evidence_id, count in Counter(
            item.evidence_id for item in items
        ).items()
        if count > 1
    }
    if duplicate_ids:
        raise ValueError(
            "Duplicate evidence IDs cannot be mapped: "
            + ", ".join(sorted(duplicate_ids))
            + "."
        )
    for item in items:
        if not item.evidence_id.strip():
            raise ValueError("Evidence IDs must be non-empty before exposure mapping.")
        if item.confidence not in QUALITATIVE_CONFIDENCE_VALUES:
            raise ValueError(
                f"Evidence {item.evidence_id!r} has invalid confidence "
                f"{item.confidence!r}."
            )

    assumptions = _annotation_map(
        assumptions_by_exposure, field_name="assumptions_by_exposure"
    )
    outcomes = _annotation_map(
        related_technical_outcomes_by_exposure,
        field_name="related_technical_outcomes_by_exposure",
    )
    declared_coverage = _coverage_map(coverage_by_category)

    grouped: dict[
        tuple[ExposureCategory, ExposureType], list[EvidenceItem]
    ] = defaultdict(list)
    warnings: list[ExposureMappingWarning] = []
    unmapped: set[str] = set()
    unmapped_by_category: dict[ExposureCategory, set[str]] = {
        category: set() for category in ExposureCategory
    }

    for evidence in sorted(items, key=lambda item: item.evidence_id):
        categories, automatic_types, unsupported_parts = _category_parts(evidence)
        for unsupported in unsupported_parts:
            warnings.append(
                ExposureMappingWarning(
                    code="unsupported_evidence_category",
                    evidence_id=evidence.evidence_id,
                    category=(
                        next(iter(categories)) if len(categories) == 1 else None
                    ),
                    requested_exposure_type=None,
                    message=(
                        f"Evidence category part {unsupported!r} is not in the "
                        "deterministic mapping policy."
                    ),
                )
            )

        claim_types = {
            exposure_type
            for pattern, exposure_type in _CLAIM_MECHANISM_RULES
            if pattern.search(evidence.claim)
        }
        candidate_types = set(automatic_types) | claim_types

        for requested in _explicit_type_values(
            evidence.evidence_id, exposure_types_by_evidence
        ):
            try:
                exposure_type = (
                    requested
                    if isinstance(requested, ExposureType)
                    else ExposureType(requested)
                )
            except (TypeError, ValueError):
                warnings.append(
                    ExposureMappingWarning(
                        code="unsupported_forced_mapping",
                        evidence_id=evidence.evidence_id,
                        category=(
                            next(iter(categories))
                            if len(categories) == 1
                            else None
                        ),
                        requested_exposure_type=str(requested),
                        message=(
                            f"Requested exposure type {requested!r} is not in "
                            "the controlled vocabulary."
                        ),
                    )
                )
                continue
            requested_category = EXPOSURE_TYPE_CATEGORY[exposure_type]
            if categories and requested_category not in categories:
                warnings.append(
                    ExposureMappingWarning(
                        code="unsupported_forced_mapping",
                        evidence_id=evidence.evidence_id,
                        category=(
                            next(iter(categories))
                            if len(categories) == 1
                            else None
                        ),
                        requested_exposure_type=exposure_type.value,
                        message=(
                            f"Requested {exposure_type.value!r} is incompatible "
                            "with the evidence category declaration."
                        ),
                    )
                )
                continue
            candidate_types.add(exposure_type)

        compatible_types = {
            exposure_type
            for exposure_type in candidate_types
            if (
                not categories
                or EXPOSURE_TYPE_CATEGORY[exposure_type] in categories
            )
        }
        if not compatible_types:
            unmapped.add(evidence.evidence_id)
            for category in categories:
                unmapped_by_category[category].add(evidence.evidence_id)
            warnings.append(
                ExposureMappingWarning(
                    code="ambiguous_exposure_mechanism",
                    evidence_id=evidence.evidence_id,
                    category=(
                        next(iter(categories)) if len(categories) == 1 else None
                    ),
                    requested_exposure_type=None,
                    message=(
                        "Evidence did not identify a defensible specific "
                        "exposure mechanism and remains unmapped."
                    ),
                )
            )
            continue

        for exposure_type in sorted(compatible_types, key=_type_order):
            category = EXPOSURE_TYPE_CATEGORY[exposure_type]
            grouped[(category, exposure_type)].append(evidence)

    exposures = tuple(
        _build_exposure(
            category,
            exposure_type,
            grouped[(category, exposure_type)],
            explicit_assumptions=assumptions[exposure_type],
            related_technical_outcomes=outcomes[exposure_type],
        )
        for category in ExposureCategory
        for exposure_type in ExposureType
        if (category, exposure_type) in grouped
    )

    exposure_ids_by_category = {
        category: tuple(
            item.exposure_id for item in exposures if item.category is category
        )
        for category in ExposureCategory
    }
    evidence_ids_by_category = {
        category: _ordered_unique(
            tuple(
                evidence.evidence_id
                for (group_category, _), group_items in grouped.items()
                if group_category is category
                for evidence in group_items
            )
        )
        for category in ExposureCategory
    }
    coverage: list[ExposureCategoryCoverage] = []
    for category in ExposureCategory:
        exposure_ids = exposure_ids_by_category[category]
        declared = declared_coverage.get(category)
        notes: list[str] = []
        if exposure_ids:
            state = ExposureCoverageState.EXPOSURES_IDENTIFIED
            if declared not in (None, ExposureCoverageState.EXPOSURES_IDENTIFIED):
                warnings.append(
                    ExposureMappingWarning(
                        code="coverage_declaration_conflict",
                        evidence_id=evidence_ids_by_category[category][0],
                        category=category,
                        requested_exposure_type=None,
                        message=(
                            f"Coverage declared {declared.value!r}, but specific "
                            "exposure mechanisms were identified."
                        ),
                    )
                )
                notes.append("Coverage state was derived from identified exposures.")
        else:
            state = declared or ExposureCoverageState.UNDECLARED
            if state is ExposureCoverageState.UNDECLARED:
                notes.append("Research coverage was not declared.")
        coverage.append(
            ExposureCategoryCoverage(
                category=category,
                state=state,
                exposure_ids=exposure_ids,
                unmapped_evidence_ids=_ordered_unique(
                    tuple(unmapped_by_category[category])
                ),
                notes=_ordered_unique(notes),
            )
        )

    warnings.sort(
        key=lambda item: (
            item.evidence_id,
            item.code,
            item.requested_exposure_type or "",
            item.message,
        )
    )
    return ExposureMappingResult(
        mapping_version=EXPOSURE_MAPPING_VERSION,
        exposures=exposures,
        coverage=tuple(coverage),
        unmapped_evidence_ids=_ordered_unique(tuple(unmapped)),
        warnings=tuple(warnings),
    )


__all__ = [
    "EVIDENCE_CATEGORY_MECHANISMS",
    "EXPOSURE_CONFIDENCE_VERSION",
    "EXPOSURE_MAPPING_VERSION",
    "ExposureMappingResult",
    "aggregate_exposure_confidence",
    "map_evidence_to_exposures",
]
