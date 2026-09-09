"""EWO, impulse verification, and correction forecasting for Elliott wave rows."""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from elliott_ai.correction_semantics import preserve_structural_hypotheses
from elliott_ai.indicators import calculate_ewo as calculate_canonical_ewo


IMPULSE_WEAK_WARNING = "Warning: Wave 3 weakest in supporting indicators"
IMPULSE_HEALTHY = "Passed: Healthy corrections with EWO zero-cross"
IMPULSE_DIVERGENCE = (
    "Valid: Wave 5 Volume/EWO Divergence Confirmed (Reversal Imminent)"
)
IMPULSE_STRENGTH_FAILED = "Failed: Wave 3 strength requirement"
IMPULSE_UNCONFIRMED = "Unconfirmed: Impulse conditions incomplete"
INSUFFICIENT_DATA = "Insufficient Data"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _wave_number(value: Any) -> int | None:
    match = re.search(r"([1-5])", str(value))
    return int(match.group(1)) if match else None


def _is_completed(row: Mapping[str, Any]) -> bool:
    status = str(row.get("wave_status", "completed")).lower()
    return status.startswith("completed") or status in {"locked", "confirmed"}


def calculate_ewo(
    dataframe: Any,
    *,
    high_column: str = "high",
    low_column: str = "low",
) -> Any:
    """Return a dataframe with median price, SMA(5), SMA(35), and EWO."""
    missing = [name for name in (high_column, low_column) if name not in dataframe.columns]
    if missing:
        raise KeyError("Missing OHLC columns: " + ", ".join(missing))

    result = dataframe.copy()
    calculation = calculate_canonical_ewo(
        result[high_column].tolist(), result[low_column].tolist()
    )
    result["median_price"] = calculation["median_price"]
    result["ewo_sma_5"] = calculation["fast_sma"]
    result["ewo_sma_35"] = calculation["slow_sma"]
    result["ewo"] = calculation["values"]
    return result


def aggregate_wave_metrics(
    candles: Any,
    wave_rows: Any,
    *,
    date_column: str = "date",
    volume_column: str = "volume",
) -> Any:
    """Add EWO range/peak and cumulative volume to completed wave rows."""
    import pandas as pd

    enriched = calculate_ewo(candles)
    if date_column in enriched.columns:
        candle_dates = pd.to_datetime(enriched[date_column], utc=True)
    else:
        candle_dates = pd.to_datetime(enriched.index, utc=True)

    is_dataframe = hasattr(wave_rows, "to_dict") and hasattr(wave_rows, "copy")
    rows = wave_rows.to_dict(orient="records") if is_dataframe else wave_rows
    updated = [copy.deepcopy(dict(row)) for row in rows]

    for row in updated:
        row.setdefault("wave_ewo_peak", None)
        row.setdefault("wave_ewo_min", None)
        row.setdefault("wave_ewo_max", None)
        row.setdefault("ewo_zero_touch", None)
        row.setdefault("cumulative_volume", None)
        if not _is_completed(row):
            continue

        start = pd.to_datetime(row.get("start_date"), utc=True, errors="coerce")
        end = pd.to_datetime(row.get("end_date"), utc=True, errors="coerce")
        if pd.isna(start) or pd.isna(end):
            continue

        wave_candles = enriched.loc[(candle_dates >= start) & (candle_dates <= end)]
        valid_ewo = wave_candles["ewo"].dropna()
        if not valid_ewo.empty:
            ewo_min = float(valid_ewo.min())
            ewo_max = float(valid_ewo.max())
            row["wave_ewo_min"] = ewo_min
            row["wave_ewo_max"] = ewo_max
            row["wave_ewo_peak"] = max(abs(ewo_min), abs(ewo_max))
            row["ewo_zero_touch"] = ewo_min <= 0.0 <= ewo_max

        if volume_column in wave_candles.columns:
            volume = pd.to_numeric(wave_candles[volume_column], errors="coerce")
            if volume.notna().any():
                row["cumulative_volume"] = float(volume.sum())

    if not is_dataframe:
        return updated
    result = wave_rows.copy()
    for field in (
        "wave_ewo_peak",
        "wave_ewo_min",
        "wave_ewo_max",
        "ewo_zero_touch",
        "cumulative_volume",
    ):
        result[field] = [row[field] for row in updated]
    return result


def _impulse_result(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    waves: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        number = _wave_number(row.get("wave_label"))
        if number is not None and _is_completed(row):
            if number in waves:
                return INSUFFICIENT_DATA, f"Duplicate completed Wave {number} rows."
            waves[number] = row

    missing = [number for number in range(1, 6) if number not in waves]
    if missing:
        return (
            INSUFFICIENT_DATA,
            "Completed five-wave sequence unavailable; missing Waves "
            + ", ".join(map(str, missing))
            + ".",
        )

    volumes = {n: _number(waves[n].get("cumulative_volume")) for n in waves}
    ewo = {n: _number(waves[n].get("wave_ewo_peak")) for n in waves}
    if any(value is None or value < 0 for value in (*volumes.values(), *ewo.values())):
        return INSUFFICIENT_DATA, "Cumulative volume and wave_ewo_peak are required for all five waves."

    volumes = {n: float(value) for n, value in volumes.items()}
    ewo = {n: abs(float(value)) for n, value in ewo.items()}
    notes: list[str] = []

    weakest_volume = volumes[3] < volumes[1] and volumes[3] < volumes[5]
    weakest_ewo = ewo[3] < ewo[1] and ewo[3] < ewo[5]
    if weakest_volume or weakest_ewo:
        dimensions = []
        if weakest_volume:
            dimensions.append("cumulative volume")
        if weakest_ewo:
            dimensions.append("absolute EWO momentum")
        return (
            IMPULSE_WEAK_WARNING,
            "Wave 3 is strictly weakest in "
            + " and ".join(dimensions)
            + "; recount the price structure, but do not invalidate it from indicators alone.",
        )

    strength_passed = volumes[3] > volumes[1] and ewo[3] > ewo[1]
    notes.append(
        "Wave 3 exceeds Wave 1 in cumulative volume and absolute EWO."
        if strength_passed
        else "Wave 3 does not exceed Wave 1 in both cumulative volume and absolute EWO."
    )

    zero_touch = waves[4].get("ewo_zero_touch") is True
    healthy = volumes[2] < volumes[1] and volumes[4] < volumes[3] and zero_touch
    notes.append(
        "Waves 2/4 contract in volume and Wave 4 touches or crosses EWO zero."
        if healthy
        else "The strict correction-volume and Wave 4 EWO zero-cross test did not fully pass."
    )

    start_1 = _number(waves[1].get("start_price"))
    end_1 = _number(waves[1].get("end_price"))
    end_3 = _number(waves[3].get("end_price"))
    end_5 = _number(waves[5].get("end_price"))
    if None in (start_1, end_1, end_3, end_5):
        new_extreme = False
        notes.append("Price direction/extremes are incomplete, so Wave 5 divergence cannot be verified.")
    else:
        bullish = float(end_1) > float(start_1)
        new_extreme = float(end_5) > float(end_3) if bullish else float(end_5) < float(end_3)

    divergence = new_extreme and volumes[5] < volumes[3] and ewo[5] < ewo[3]
    if divergence:
        notes.append("Wave 5 makes a new price extreme on lower cumulative volume and lower absolute EWO.")

    if divergence:
        status = IMPULSE_DIVERGENCE
    elif not strength_passed:
        status = IMPULSE_STRENGTH_FAILED
    elif healthy:
        status = IMPULSE_HEALTHY
    else:
        status = IMPULSE_UNCONFIRMED
    return status, " ".join(notes)


def add_impulse_verification(
    dataset: Any, *, sequence_fields: Sequence[str] = ("sequence_id",)
) -> tuple[Any, dict[str, Any]]:
    is_dataframe = hasattr(dataset, "to_dict") and hasattr(dataset, "copy")
    rows = dataset.to_dict(orient="records") if is_dataframe else dataset
    updated = [copy.deepcopy(dict(row)) for row in rows]
    groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, row in enumerate(updated):
        groups[tuple(row.get(field) for field in sequence_fields)].append(index)

    counts: Counter[str] = Counter()
    for indices in groups.values():
        status, notes = _impulse_result([updated[index] for index in indices])
        counts[status] += 1
        for index in indices:
            updated[index]["impulse_status"] = status
            updated[index]["impulse_notes"] = notes
    summary = {"total_sequences": len(groups), "impulse_status_counts": dict(sorted(counts.items()))}

    if not is_dataframe:
        return updated, summary
    result = dataset.copy()
    result["impulse_status"] = [row["impulse_status"] for row in updated]
    result["impulse_notes"] = [row["impulse_notes"] for row in updated]
    return result, summary


def _hypothesis(
    hypothesis_id: str,
    label: str,
    *,
    evidence_needed: Sequence[str],
) -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis_id,
        "label": label,
        "supporting_evidence": [],
        "contradictory_evidence": [],
        "unavailable_or_incomparable_evidence": [],
        "evidence_needed_for_confirmation": list(evidence_needed),
    }


def _candidate_by_id(
    hypotheses: Sequence[dict[str, Any]], hypothesis_id: str
) -> dict[str, Any]:
    return next(
        hypothesis
        for hypothesis in hypotheses
        if hypothesis["hypothesis_id"] == hypothesis_id
    )


def _provided_invalidation_evidence(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for field in ("hypothesis_invalidations", "explicit_invalidation_evidence"):
        values = record.get(field)
        if isinstance(values, list):
            evidence.extend(dict(item) for item in values if isinstance(item, Mapping))
    return evidence


def _finalize_assessment(
    hypotheses: Sequence[dict[str, Any]], record: Mapping[str, Any]
) -> dict[str, Any]:
    assessment = preserve_structural_hypotheses(
        hypotheses,
        _provided_invalidation_evidence(record),
    )
    evidence_summary = {
        field: {
            candidate["hypothesis_id"]: list(candidate.get(field, []))
            for candidate in assessment["candidate_hypotheses"]
        }
        for field in (
            "supporting_evidence",
            "contradictory_evidence",
            "unavailable_or_incomparable_evidence",
            "evidence_needed_for_confirmation",
        )
    }
    assessment["evidence_summary"] = evidence_summary
    assessment.update(evidence_summary)
    return assessment


def _assessment_legacy_label(
    assessment: Mapping[str, Any], *, ambiguous: str
) -> str:
    selected = assessment.get("selected_hypothesis")
    if isinstance(selected, Mapping):
        return str(selected.get("label") or "")
    if assessment.get("status") == "no_structurally_valid_candidate":
        return "Invalid: no structurally valid candidate remains"
    return ambiguous


def classify_early_leg_1(record: Mapping[str, Any]) -> dict[str, Any]:
    """Describe Wave A and Wave W candidates without indicator-based selection."""
    result = copy.deepcopy(dict(record))
    result["early_correction_label"] = ""
    result["confidence_score"] = None
    result["early_correction_notes"] = (
        "Classifier requires a completed three-subwave Leg 1."
    )
    result["early_correction_assessment"] = {
        "status": "not_applicable",
        "candidate_hypotheses": [],
        "selected_hypothesis": None,
        "structural_invalidations": [],
        "ignored_nonstructural_invalidations": [],
        "evidence_summary": {},
        "supporting_evidence": {},
        "contradictory_evidence": {},
        "unavailable_or_incomparable_evidence": {},
        "evidence_needed_for_confirmation": {},
    }
    if _number(record.get("leg_1_subwaves")) != 3:
        return result

    hypotheses = [
        _hypothesis(
            "corrective_wave_a",
            "Wave A candidate (corrective family unresolved)",
            evidence_needed=(
                "Leg 2 and Leg 3 must complete a valid parent corrective family.",
                "A motive or diagonal Wave C would confirm a flat-family A-B-C path.",
            ),
        ),
        _hypothesis(
            "combination_wave_w",
            "Wave W candidate (combination unfolding)",
            evidence_needed=(
                "A corrective X connector must form without breaking the parent invalidation.",
                "A subsequent corrective Y must complete a valid W-X-Y combination.",
            ),
        ),
    ]
    wave_a = _candidate_by_id(hypotheses, "corrective_wave_a")
    wave_w = _candidate_by_id(hypotheses, "combination_wave_w")

    leg_ewo = _number(record.get("leg_1_ewo_peak"))
    impulse_ewo = _number(record.get("preceding_impulse_ewo_peak"))
    if leg_ewo is not None and impulse_ewo is not None and impulse_ewo != 0:
        ratio = abs(leg_ewo) / abs(impulse_ewo)
        if ratio < 0.50:
            note = "EWO depth is below 50% of the preceding impulse."
            wave_a["supporting_evidence"].append(note)
            wave_w["contradictory_evidence"].append(note)
        elif ratio >= 0.80:
            note = "EWO depth is at least 80% of the preceding impulse."
            wave_w["supporting_evidence"].append(note)
            wave_a["contradictory_evidence"].append(note)
        else:
            note = "EWO depth lies between the legacy comparison thresholds."
            for hypothesis in hypotheses:
                hypothesis["unavailable_or_incomparable_evidence"].append(note)
    else:
        note = "EWO depth is unavailable or incomparable with the preceding impulse."
        for hypothesis in hypotheses:
            hypothesis["unavailable_or_incomparable_evidence"].append(note)

    effort = _number(record.get("effort_result_ratio"))
    historical = _number(record.get("historical_50_effort_result_avg"))
    if effort is not None and historical is not None and historical > 0:
        if effort > 1.2 * historical:
            note = "Weis effort/result is above 1.2 times its historical average."
            wave_w["supporting_evidence"].append(note)
            wave_a["contradictory_evidence"].append(note)
        elif effort < historical:
            note = "Weis effort/result is below its historical average."
            wave_a["supporting_evidence"].append(note)
            wave_w["contradictory_evidence"].append(note)
        else:
            note = "Weis effort/result lies between the legacy comparison thresholds."
            for hypothesis in hypotheses:
                hypothesis["unavailable_or_incomparable_evidence"].append(note)
    else:
        note = "Weis effort/result evidence is unavailable or incomparable."
        for hypothesis in hypotheses:
            hypothesis["unavailable_or_incomparable_evidence"].append(note)

    bollinger = record.get("leg_1_pierced_bollinger_2")
    keltner = record.get("leg_1_pierced_keltner")
    if bollinger is True or keltner is True:
        note = "Leg 1 pierced an outer Bollinger or Keltner boundary."
        wave_a["supporting_evidence"].append(note)
        wave_w["contradictory_evidence"].append(note)
    elif bollinger is False and keltner is False:
        note = "Leg 1 remained inside both supplied volatility boundaries."
        wave_w["supporting_evidence"].append(note)
        wave_a["contradictory_evidence"].append(note)
    else:
        note = "Volatility-boundary evidence is unavailable or incomparable."
        for hypothesis in hypotheses:
            hypothesis["unavailable_or_incomparable_evidence"].append(note)

    assessment = _finalize_assessment(hypotheses, record)
    result["early_correction_assessment"] = assessment
    result["early_correction_label"] = _assessment_legacy_label(
        assessment,
        ambiguous="Unresolved: Wave A versus Wave W",
    )
    result["early_correction_notes"] = (
        "Indicator observations are recorded as supporting or contradictory evidence; "
        "they do not select or invalidate either structurally valid candidate."
    )
    return result


def _is_completed_wxy(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return normalized in {"wxy", "doublethree", "completedwxy"}


def _completed_wxy_hypotheses() -> list[dict[str, Any]]:
    shared = "The supplied local structure contains a completed corrective W-X-Y."
    hypotheses = [
        _hypothesis(
            "correction_complete",
            "Completed W-X-Y is the entire correction",
            evidence_needed=(
                "A motive break in the larger trend direction must invalidate further corrective extension.",
            ),
        ),
        _hypothesis(
            "larger_wave_w",
            "Completed W-X-Y is a larger Wave W",
            evidence_needed=(
                "A corrective X must follow, then another valid corrective family must complete Wave Y.",
            ),
        ),
        _hypothesis(
            "larger_wave_a",
            "Completed W-X-Y is a larger Wave A",
            evidence_needed=(
                "A corrective Wave B and the required terminal family must establish the larger A-B-C context.",
            ),
        ),
        _hypothesis(
            "triple_three_continuation",
            "Apparent Wave Y may continue through X2-Z",
            evidence_needed=(
                "A distinct corrective X2 connector must form after Y.",
                "A final corrective Z must complete without violating the parent boundaries.",
            ),
        ),
    ]
    for hypothesis in hypotheses:
        hypothesis["supporting_evidence"].append(shared)
    return hypotheses


def forecast_correction(record: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve structurally valid correction forecasts until price resolves them."""
    result = copy.deepcopy(dict(record))
    result.update(
        {
            "correction_structure_label": INSUFFICIENT_DATA,
            "leg_2_forecast": "",
            "leg_3_forecast": "",
            "final_leg_label": "",
            "correction_forecast_notes": "Leg 1 subwave count or completed local structure is required.",
            "correction_assessment": {
                "status": "not_applicable",
                "candidate_hypotheses": [],
                "selected_hypothesis": None,
                "structural_invalidations": [],
                "ignored_nonstructural_invalidations": [],
                "evidence_summary": {},
                "supporting_evidence": {},
                "contradictory_evidence": {},
                "unavailable_or_incomparable_evidence": {},
                "evidence_needed_for_confirmation": {},
            },
        }
    )
    subwaves = _number(record.get("leg_1_subwaves"))
    retracement = _number(record.get("leg_2_retracement_pct"))

    if _is_completed_wxy(record.get("completed_local_structure")):
        hypotheses = _completed_wxy_hypotheses()
        continuation = _candidate_by_id(hypotheses, "triple_three_continuation")
        if record.get("second_shallow_pullback") is True:
            continuation["supporting_evidence"].append(
                "A possible second corrective connector is present."
            )
        else:
            continuation["unavailable_or_incomparable_evidence"].append(
                "A distinct X2 connector has not yet been structurally confirmed."
            )
        if record.get("second_pullback_low_volume") is True:
            continuation["supporting_evidence"].append(
                "Low connector volume is compatible with, but does not prove, X2."
            )
        elif record.get("second_pullback_low_volume") is None:
            continuation["unavailable_or_incomparable_evidence"].append(
                "Comparable X2 volume evidence is unavailable."
            )
        ambiguous_label = (
            "Unresolved: completed W-X-Y, larger W, larger A, or X2-Z continuation"
        )
    elif subwaves == 5:
        hypotheses = [
            _hypothesis(
                "new_motive_wave_1",
                "Wave 1 of a new motive sequence",
                evidence_needed=(
                    "A corrective Wave 2 must hold above the Wave 1 origin.",
                    "A subsequent motive Wave 3 must confirm continuation in the same direction.",
                ),
            ),
            _hypothesis(
                "zigzag_wave_a",
                "Wave A of a Zigzag (5-3-5)",
                evidence_needed=(
                    "A corrective three-wave B must form.",
                    "A motive or allowed diagonal C must complete the zigzag.",
                ),
            ),
        ]
        for hypothesis in hypotheses:
            hypothesis["supporting_evidence"].append(
                "Leg 1 has a completed five-wave motive structure."
            )
        ambiguous_label = "Unresolved: Wave 1 versus Wave A"
    elif subwaves == 3:
        hypotheses = [
            _hypothesis(
                "flat_wave_a",
                "Wave A of a Flat (3-3-5)",
                evidence_needed=(
                    "A corrective B and a motive or allowed diagonal C must complete a valid flat.",
                ),
            ),
            _hypothesis(
                "triangle_wave_a",
                "Wave A of a Triangle (3-3-3-3-3)",
                evidence_needed=(
                    "Four additional corrective legs B-C-D-E must remain inside a valid triangle context.",
                ),
            ),
            _hypothesis(
                "combination_wave_w",
                "Wave W of a W-X-Y combination",
                evidence_needed=(
                    "A corrective X and a valid corrective Y must complete the combination.",
                ),
            ),
        ]
        flat = _candidate_by_id(hypotheses, "flat_wave_a")
        triangle = _candidate_by_id(hypotheses, "triangle_wave_a")
        combination = _candidate_by_id(hypotheses, "combination_wave_w")
        if retracement is not None and 90.0 <= retracement <= 138.2:
            note = "Leg 2 retraces between 90% and 138.2% of Leg 1."
            flat["supporting_evidence"].append(note)
            combination["contradictory_evidence"].append(note)
        elif retracement is not None and retracement < 90.0:
            note = "Leg 2 retraces less than 90% of Leg 1."
            combination["supporting_evidence"].append(note)
            flat["contradictory_evidence"].append(note)
            triangle["supporting_evidence"].append(
                "A contained retracement is compatible with a developing triangle."
            )
        elif retracement is None:
            for hypothesis in hypotheses:
                hypothesis["unavailable_or_incomparable_evidence"].append(
                    "Leg 2 retracement is unavailable."
                )
        else:
            for hypothesis in hypotheses:
                hypothesis["contradictory_evidence"].append(
                    "Leg 2 exceeds the legacy 138.2% comparison range."
                )
        ambiguous_label = "Unresolved: flat A, triangle A, or combination W"
    else:
        return result

    assessment = _finalize_assessment(hypotheses, record)
    result["correction_assessment"] = assessment
    result["correction_structure_label"] = _assessment_legacy_label(
        assessment,
        ambiguous=ambiguous_label,
    )
    selected = assessment.get("selected_hypothesis")
    if isinstance(selected, Mapping):
        result["leg_2_forecast"] = "; ".join(
            str(item) for item in selected.get("evidence_needed_for_confirmation", [])[:1]
        )
        result["leg_3_forecast"] = "; ".join(
            str(item) for item in selected.get("evidence_needed_for_confirmation", [])[1:]
        )
        if selected.get("hypothesis_id") == "triple_three_continuation":
            result["final_leg_label"] = "Wave Z"
        elif selected.get("hypothesis_id") in {"zigzag_wave_a", "flat_wave_a"}:
            result["final_leg_label"] = "Wave C"
        elif selected.get("hypothesis_id") == "combination_wave_w":
            result["final_leg_label"] = "Wave Y"
    result["correction_forecast_notes"] = (
        "All structurally valid candidates remain unresolved unless a hard Elliott rule "
        "or explicit price invalidation removes them. Indicator and retracement behavior "
        "is supporting or contradictory evidence only."
    )
    return result


def add_correction_classification(dataset: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [forecast_correction(classify_early_leg_1(row)) for row in dataset]


def _engine_rules() -> dict[str, Any]:
    return {
        "source": "User-requested analytical upgrade, integrated 2026-07-14",
        "EWO_CalculationLayer": {
            "median_price": "(High + Low) / 2",
            "ewo": "SMA(5, Median Price) - SMA(35, Median Price)",
            "wave_fields": ["cumulative_volume", "wave_ewo_peak", "wave_ewo_min", "wave_ewo_max", "ewo_zero_touch"],
            "aggregation_rule": "Only completed waves are aggregated; wave_ewo_peak is max(abs(EWO)) inside inclusive parent dates.",
        },
        "ImpulseVerificationEngine": {
            "wave_3_strength": "Wave 3 cumulative volume and abs EWO peak commonly exceed Wave 1; weakness is a supporting-evidence warning, not a classical invalidation.",
            "correction_reset": "W2 volume < W1, W4 volume < W3, and Wave 4 EWO range must touch/cross zero.",
            "wave_5_divergence": "A new Wave 5 price extreme requires both cumulative volume and abs EWO peak below Wave 3.",
            "signal_boundary": "Reversal Imminent is a compatibility label, not standalone trade timing; require structure, price confirmation, invalidation, and risk controls.",
        },
        "EarlyLeg1CorrectionClassifier": {
            "trigger": "leg_1_subwaves == 3",
            "candidate_rule": "Preserve structurally valid Wave A and Wave W hypotheses until hard structure or an explicit price invalidation distinguishes them.",
            "evidence_rule": "EWO, Weis effort/result, and volatility boundaries are supporting, contradictory, or unavailable evidence; they never invalidate by themselves.",
            "confidence_score": "Retained as a legacy output field and always left null; no fixed probability is manufactured.",
        },
        "ForensicCorrectionForecaster": {
            "five_subwaves": "Preserve Wave 1 and zigzag Wave A until later price structure distinguishes them.",
            "three_subwaves": "Preserve structurally valid flat-A, triangle-A, and combination-W hypotheses; retracement behavior ranks evidence only.",
            "completed_wxy": "Preserve complete correction, larger W, larger A, and possible X2-Z continuation while each remains structurally valid.",
            "triple_three": "Use distinct W-X-Y-X2-Z positions; a second shallow or low-volume connector supports X2 but does not prove it.",
            "invalidation_rule": "Only hard Elliott structure or an explicit price invalidation can remove a candidate.",
        },
    }


def apply_to_database(database_path: Path, report_path: Path) -> dict[str, Any]:
    database = json.loads(database_path.read_text(encoding="utf-8"))
    schema = database.setdefault("10_OutputSchemaTemplate", {})
    for field, default in {
        "cumulative_volume": 0.0,
        "wave_ewo_peak": None,
        "wave_ewo_min": None,
        "wave_ewo_max": None,
        "ewo_zero_touch": None,
        "impulse_status": "",
        "impulse_notes": "",
        "leg_1_subwaves": None,
        "leg_1_ewo_peak": None,
        "preceding_impulse_ewo_peak": None,
        "effort_result_ratio": None,
        "historical_50_effort_result_avg": None,
        "leg_1_pierced_bollinger_2": None,
        "leg_1_pierced_keltner": None,
        "early_correction_label": "",
        "confidence_score": None,
        "leg_2_retracement_pct": None,
        "correction_structure_label": "",
        "leg_2_forecast": "",
        "leg_3_forecast": "",
        "final_leg_label": "",
    }.items():
        schema.setdefault(field, default)

    database["16_AdvancedAnalyticalLayers"] = _engine_rules()
    records = database.get("11_ActiveDatabaseRecords", [])
    if not isinstance(records, list):
        raise TypeError("11_ActiveDatabaseRecords must be a JSON array.")

    migrated = [copy.deepcopy(dict(row)) for row in records]
    for row in migrated:
        row.setdefault("cumulative_volume", row.get("total_volume"))
        for field in ("wave_ewo_peak", "wave_ewo_min", "wave_ewo_max", "ewo_zero_touch"):
            row.setdefault(field, None)
        if (
            row.get("sequence_id") == "NYSE_DELL_PRIMARY_III_INTERMEDIATE_2022_2026"
            and _wave_number(row.get("wave_label")) == 3
        ):
            row["wave_status"] = "active_candidate"
            row["observed_extreme_price"] = row.get("end_price")
            row["observed_extreme_date"] = row.get("end_date")
            row["analysis_notes"] = (
                "Lower-timeframe weekly validation on 2026-07-14 indicates the 469.47 high is "
                "more likely Minor Wave 3 inside an active Intermediate Wave (3); Minor Wave 4 "
                "is developing and Minor Wave 5 is not yet confirmed."
            )

    migrated, impulse_summary = add_impulse_verification(migrated)
    migrated = add_correction_classification(migrated)
    database["11_ActiveDatabaseRecords"] = migrated
    database_path.write_text(json.dumps(database, indent=2) + "\n", encoding="utf-8")

    ewo_pending = sum(row.get("wave_ewo_peak") is None for row in migrated)
    report = {
        "records_updated": len(migrated),
        "ewo_pending_raw_ohlcv": ewo_pending,
        **impulse_summary,
        "correction_records_classified": sum(bool(row.get("correction_structure_label") and row.get("correction_structure_label") != INSUFFICIENT_DATA) for row in migrated),
        "database_revision": "DELL Intermediate Wave (3) changed from completed_candidate to active_candidate after weekly subdivision validation.",
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("AI_BRAIN_MASTER_RULES.json"))
    parser.add_argument("--report", type=Path, default=Path("advanced_analysis_report.json"))
    args = parser.parse_args()
    print(json.dumps(apply_to_database(args.database, args.report), indent=2))


if __name__ == "__main__":
    main()
